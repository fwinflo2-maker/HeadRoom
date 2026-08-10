/**
 * HeadroomContextEngine — ContextEngine implementation for OpenClaw.
 *
 * Compresses tool outputs and conversation context using the Headroom proxy.
 * Zero LLM calls — all compression is algorithmic (SmartCrusher, ContentRouter, etc.)
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { readFile, rename, rm, writeFile } from "node:fs/promises";

import { compress } from "headroom-ai";
import { ProxyManager, defaultLogger, type ProxyManagerConfig, type ProxyManagerLogger } from "./proxy-manager.js";
import { agentToOpenAI, normalizeAgentMessages, openAIToAgent } from "./convert.js";

export interface HeadroomEngineConfig extends ProxyManagerConfig {
  enabled?: boolean;
}

export class HeadroomContextEngine {
  readonly info = {
    id: "headroom",
    name: "Headroom Context Compression",
    version: "0.1.0",
    ownsCompaction: true,
  };

  private proxyManager: ProxyManager;
  private proxyUrl: string | null = null;
  private config: HeadroomEngineConfig;
  private logger: ProxyManagerLogger;
  private proxyReadyListeners = new Set<(proxyUrl: string) => void | Promise<void>>();
  private proxyStartupPromise: Promise<string> | null = null;
  private proxyStartupError: unknown = null;
  private stats = {
    totalCompressions: 0,
    totalTokensSaved: 0,
    totalTokensBefore: 0,
    compactions: 0,
  };

  constructor(config: HeadroomEngineConfig = {}, logger?: ProxyManagerLogger) {
    this.config = config;
    this.logger = logger ?? defaultLogger;
    this.proxyManager = new ProxyManager(config, this.logger);
  }

  // === ContextEngine Lifecycle ===

  async bootstrap(params: {
    sessionId: string;
    sessionKey?: string;
    sessionFile: string;
  }): Promise<{ bootstrapped: boolean; reason?: string }> {
    if (this.config.enabled === false) {
      return { bootstrapped: false, reason: "disabled" };
    }

    this.ensureProxyStarted();
    return { bootstrapped: true, reason: "proxy startup scheduled" };
  }

  async ingest(params: {
    sessionId: string;
    message: any;
    isHeartbeat?: boolean;
  }): Promise<{ ingested: boolean }> {
    // No-op: OpenClaw's runtime stores messages. We don't need a separate store.
    return { ingested: true };
  }

  async ingestBatch?(params: {
    sessionId: string;
    messages: any[];
    isHeartbeat?: boolean;
  }): Promise<{ ingestedCount: number }> {
    return { ingestedCount: params.messages.length };
  }

  /**
   * Assemble context for the model — THE CORE HOOK.
   *
   * Converts AgentMessage[] → OpenAI format → compress() → AgentMessage[]
   */
  async assemble(params: {
    sessionId: string;
    messages: any[];
    tokenBudget?: number;
    model?: string;
    prompt?: string;
  }): Promise<{
    messages: any[];
    estimatedTokens: number;
    systemPromptAddition?: string;
  }> {
    if (!this.proxyUrl || this.config.enabled === false) {
      this.ensureProxyStarted();
      // Fallback: return messages unchanged
      return { messages: normalizeAgentMessages(params.messages), estimatedTokens: 0 };
    }

    try {
      // Convert AgentMessage → OpenAI format
      const openaiMessages = agentToOpenAI(params.messages);

      // Compress via proxy — pass tokenBudget so the pipeline can tune its
      // context-pressure decisions for the target model.
      const result = await compress(openaiMessages, {
        model: params.model ?? "claude-sonnet-4-5",
        baseUrl: this.proxyUrl,
        fallback: true,
        tokenBudget: params.tokenBudget,
      } as any);

      if (!result.compressed || result.tokensSaved === 0) {
        return {
          messages: normalizeAgentMessages(params.messages),
          estimatedTokens: result.tokensBefore,
        };
      }

      // Convert back to AgentMessage format
      const compressedAgentMessages = openAIToAgent(result.messages);

      // Track stats
      this.stats.totalCompressions++;
      this.stats.totalTokensSaved += result.tokensSaved;
      this.stats.totalTokensBefore += result.tokensBefore;

      this.logger.debug(
        `Assembled: ${result.tokensBefore} → ${result.tokensAfter} tokens (saved ${result.tokensSaved})`,
      );

      return {
        messages: compressedAgentMessages,
        estimatedTokens: result.tokensAfter,
        systemPromptAddition:
          result.tokensSaved > 100
            ? `[Context compressed by Headroom: ${result.tokensSaved} tokens saved. Use headroom_retrieve with the hash to get full details.]`
            : undefined,
      };
    } catch (error) {
      this.logger.error(`Assemble failed: ${error}`);
      // Graceful fallback: return original messages
      return { messages: normalizeAgentMessages(params.messages), estimatedTokens: 0 };
    }
  }

  /**
   * Compact context — zero-cost alternative to LLM summarization.
   *
   * Calls compress() with the token budget, which triggers:
   * - SmartCrusher: aggressive JSON compression (70-90% on tool outputs)
   * - Kompress: ModernBERT text compression (40-60% on assistant text)
   * - Pipeline: compresses message content in place without dropping history
   * - CCR: stores originals for retrieval via headroom_retrieve tool
   *
   * Zero LLM calls. All algorithmic. The compacted message records are written
   * back to the supplied OpenClaw JSONL session file.
   */
  async compact(params: {
    sessionId: string;
    sessionFile: string;
    tokenBudget?: number;
    force?: boolean;
    runtimeContext?: any;
  }): Promise<{
    ok: boolean;
    compacted: boolean;
    reason?: string;
    result?: {
      tokensBefore: number;
      tokensAfter?: number;
    };
  }> {
    if (!this.proxyUrl) {
      return { ok: false, compacted: false, reason: "Proxy not available" };
    }

    try {
      const sessionContent = await readFile(params.sessionFile, "utf8");
      const lineEnding = sessionContent.includes("\r\n") ? "\r\n" : "\n";
      const lines = sessionContent.split(/\r?\n/);
      const records: Array<{ index: number; value: Record<string, any> }> = [];

      for (const [index, line] of lines.entries()) {
        if (!line.trim()) continue;

        let value: Record<string, any>;
        try {
          value = JSON.parse(line) as Record<string, any>;
        } catch (error) {
          throw new Error(`Invalid JSONL record at line ${index + 1}: ${error}`);
        }

        if (
          value.type === "message" &&
          value.message !== null &&
          typeof value.message === "object" &&
          !Array.isArray(value.message)
        ) {
          records.push({ index, value });
        }
      }

      if (records.length === 0) {
        return { ok: true, compacted: false, reason: "Session contains no messages" };
      }

      const result = await compress(
        agentToOpenAI(records.map(({ value }) => value.message)),
        {
          model: "claude-sonnet-4-5",
          baseUrl: this.proxyUrl,
          fallback: true,
          tokenBudget: params.tokenBudget,
        } as any,
      );

      const compactedMessages = openAIToAgent(result.messages);
      if (!result.compressed || result.tokensSaved === 0) {
        return {
          ok: true,
          compacted: false,
          reason: "Session did not need compression",
          result: {
            tokensBefore: result.tokensBefore,
            tokensAfter: result.tokensAfter,
          },
        };
      }

      // The transcript is append-only and can contain non-message records.
      // Refuse to rewrite if compression changes the message count, because
      // there is no safe way to preserve the transcript's record envelopes.
      if (compactedMessages.length !== records.length) {
        return {
          ok: false,
          compacted: false,
          reason: "Compression changed the session message count",
          result: {
            tokensBefore: result.tokensBefore,
            tokensAfter: result.tokensAfter,
          },
        };
      }

      const outputLines = [...lines];
      records.forEach(({ index, value }, messageIndex) => {
        outputLines[index] = JSON.stringify({
          ...value,
          message: {
            ...value.message,
            ...compactedMessages[messageIndex],
          },
        });
      });

      // Replace the transcript atomically so a failed write cannot leave a
      // partially compacted session that OpenClaw can no longer parse.
      const temporaryFile = `${params.sessionFile}.headroom.tmp`;
      try {
        await writeFile(temporaryFile, outputLines.join(lineEnding), "utf8");
        await rename(temporaryFile, params.sessionFile);
      } finally {
        await rm(temporaryFile, { force: true }).catch(() => undefined);
      }

      this.stats.compactions++;
      this.logger.info(
        `Compacted session (budget: ${params.tokenBudget ?? "none"}, ` +
          `force: ${params.force ?? false}, saved: ${result.tokensSaved} tokens)`,
      );

      return {
        ok: true,
        compacted: true,
        reason: "Compacted session with Headroom",
        result: {
          tokensBefore: result.tokensBefore,
          tokensAfter: result.tokensAfter,
        },
      };
    } catch (error) {
      this.logger.error(`Compact failed: ${error}`);
      return { ok: false, compacted: false, reason: `Compaction failed: ${error}` };
    }
  }

  async afterTurn?(params: {
    sessionId: string;
    messages: any[];
    prePromptMessageCount: number;
    isHeartbeat?: boolean;
  }): Promise<void> {
    // Optional: could log stats or trigger learning
  }

  async prepareSubagentSpawn?(params: {
    parentSessionKey: string;
    childSessionKey: string;
    ttlMs?: number;
  }): Promise<{ rollback: () => Promise<void> } | undefined> {
    // Subagent context is compressed naturally via assemble()
    return undefined;
  }

  async onSubagentEnded?(params: {
    childSessionKey: string;
    reason: string;
  }): Promise<void> {
    // No-op
  }

  async dispose(): Promise<void> {
    await this.proxyManager.stop();
    this.logger.info(
      `Engine disposed. Stats: ${this.stats.totalCompressions} compressions, ` +
        `${this.stats.totalTokensSaved} tokens saved`,
    );
  }

  // --- Public API ---

  getStats() {
    return { ...this.stats };
  }

  getProxyUrl(): string | null {
    return this.proxyUrl;
  }

  getProxyStartupError(): unknown {
    return this.proxyStartupError;
  }

  ensureProxyStarted(): void {
    if (this.config.enabled === false || this.proxyUrl || this.proxyStartupPromise) {
      return;
    }

    this.proxyStartupError = null;
    this.proxyStartupPromise = this.proxyManager
      .start()
      .then(async (proxyUrl) => {
        this.proxyUrl = proxyUrl;
        this.proxyStartupError = null;
        await this.notifyProxyReady(proxyUrl);
        this.logger.info(`Headroom proxy ready at ${proxyUrl}`);
        return proxyUrl;
      })
      .catch((error) => {
        this.proxyStartupError = error;
        this.logger.warn(`Headroom proxy unavailable: ${error}`);
        throw error;
      })
      .finally(() => {
        this.proxyStartupPromise = null;
      });

    // Fire-and-forget lifecycle callers intentionally do not await this promise.
    // Keep the promise rejectable for ensureProxyUrl(), but mark it observed so
    // a missing proxy cannot become a process-level unhandled rejection.
    void this.proxyStartupPromise.catch(() => {});
  }

  onProxyReady(listener: (proxyUrl: string) => void | Promise<void>): () => void {
    this.proxyReadyListeners.add(listener);
    return () => {
      this.proxyReadyListeners.delete(listener);
    };
  }

  async ensureProxyUrl(): Promise<string> {
    if (this.proxyUrl) {
      return this.proxyUrl;
    }

    this.ensureProxyStarted();
    if (!this.proxyStartupPromise) {
      throw new Error("Headroom proxy startup is disabled");
    }
    return this.proxyStartupPromise;
  }

  private async notifyProxyReady(proxyUrl: string): Promise<void> {
    for (const listener of this.proxyReadyListeners) {
      try {
        await listener(proxyUrl);
      } catch (error) {
        this.logger.warn(`Headroom proxy ready listener failed: ${error}`);
      }
    }
  }
}
