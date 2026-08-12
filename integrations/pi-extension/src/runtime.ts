import type { ToolResultMessage } from "@earendil-works/pi-ai";
import type {
  ContextEvent,
  ExtensionContext,
  ToolResultEvent,
} from "@earendil-works/pi-coding-agent";

import { PreparedCache } from "./cache.js";
import type { HeadroomConfig } from "./config.js";
import { candidateFromToolResult, transformContext } from "./policy.js";
import type { RuntimeSnapshot, TransformStats } from "./status.js";
import { formatStatus } from "./status.js";
import {
  CompressionWorker,
  type WorkerClient,
  type WorkerState,
} from "./worker.js";

export interface HeadroomRuntimeOptions {
  config: HeadroomConfig;
  client: WorkerClient;
  cache?: PreparedCache;
  warnings?: string[];
}

interface ModelEligibility {
  eligible: boolean;
  modelId: string;
}

function emptyTransformStats(): TransformStats {
  return {
    substitutions: 0,
    tokensBefore: 0,
    tokensAfter: 0,
    tokensSaved: 0,
    bytesBefore: 0,
    bytesAfter: 0,
    bytesSaved: 0,
  };
}

export class HeadroomRuntime {
  readonly #config: HeadroomConfig;
  readonly #client: WorkerClient;
  readonly #cache: PreparedCache;
  readonly #worker: CompressionWorker;
  readonly #warnings: string[];
  #sessionEnabled: boolean;
  #context: ExtensionContext | undefined;
  #activity = false;
  #warningsShown = false;
  #tokensSaved = 0;
  #tokensBefore = 0;
  #tokensAfter = 0;
  #bytesSaved = 0;
  #retrievals = 0;
  #lastTransform = emptyTransformStats();
  #modelId = "unknown";

  constructor(options: HeadroomRuntimeOptions) {
    this.#config = {
      ...options.config,
      protectedTools: [...options.config.protectedTools],
    };
    this.#client = options.client;
    this.#cache = options.cache ?? new PreparedCache(options.config.maxCacheBytes);
    this.#warnings = [...(options.warnings ?? [])];
    this.#sessionEnabled = options.config.enabled;
    this.#worker = new CompressionWorker({
      client: options.client,
      cache: this.#cache,
      onAccepted: (entry) => {
        this.#tokensBefore += entry.tokensBefore;
        this.#tokensAfter += entry.tokensAfter;
        this.#bytesSaved += Math.max(
          0,
          entry.originalBytes - entry.compressedBytes,
        );
        this.#tokensSaved += entry.tokensSaved;
        this.#activity = true;
        this.#updateStatus();
      },
      onRejected: () => {
        this.#activity = true;
        this.#updateStatus();
      },
      onStateChange: (state) => this.#onStateChange(state),
    });
  }

  start(ctx: ExtensionContext): void {
    this.#context = ctx;
    if (!this.#warningsShown && this.#warnings.length > 0 && ctx.hasUI) {
      this.#warningsShown = true;
      try {
        ctx.ui.notify(this.#warnings.join("\n"), "warning");
      } catch {
        // Host UI failures cannot affect context handling.
      }
    }
    void this.#worker.checkHealth().catch(() => undefined);
  }

  observeToolResult(
    event: ToolResultEvent,
    ctx: ExtensionContext,
  ): undefined {
    if (!this.#sessionEnabled) return undefined;
    this.#context = ctx;
    const model = this.#modelEligibility(ctx);
    this.#modelId = model.modelId;
    if (!model.eligible) return undefined;

    const message: ToolResultMessage = {
      role: "toolResult",
      toolCallId: event.toolCallId,
      toolName: event.toolName,
      content: event.content,
      details: event.details,
      isError: event.isError,
      timestamp: Date.now(),
    };
    const candidate = candidateFromToolResult(message, this.#config);
    if (candidate) {
      this.#activity = true;
      this.#worker.enqueue(candidate, model.modelId);
      this.#updateStatus();
    }
    return undefined;
  }

  transform(
    messages: ContextEvent["messages"],
    ctx: ExtensionContext,
  ): ContextEvent["messages"] | undefined {
    const lastTransform = emptyTransformStats();
    this.#lastTransform = lastTransform;
    if (!this.#sessionEnabled) return undefined;
    this.#context = ctx;
    const model = this.#modelEligibility(ctx);
    this.#modelId = model.modelId;
    if (!model.eligible) return undefined;

    return transformContext(
      messages,
      this.#config,
      (key) => this.#cache.get(key),
      (candidate) => {
        this.#activity = true;
        this.#worker.enqueue(candidate, model.modelId);
      },
      (entry) => {
        lastTransform.substitutions += 1;
        lastTransform.tokensBefore += entry.tokensBefore;
        lastTransform.tokensAfter += entry.tokensAfter;
        lastTransform.tokensSaved += entry.tokensSaved;
        lastTransform.bytesBefore += entry.originalBytes;
        lastTransform.bytesAfter += entry.compressedBytes;
        lastTransform.bytesSaved += Math.max(
          0,
          entry.originalBytes - entry.compressedBytes,
        );
      },
    );
  }

  async retrieve(
    hash: string,
    signal?: AbortSignal,
  ): Promise<string> {
    this.#retrievals += 1;
    this.#activity = true;
    this.#updateStatus();
    const local = this.#cache.getRetrieval(hash);
    if (local !== undefined) return local;

    try {
      const remote = await this.#client.retrieve(hash, signal);
      if (remote !== undefined) return remote;
    } catch {
      // Retrieval failure is an explicit miss, never a host failure.
    }
    return `Headroom retrieval miss for ${hash}. Rerun the originating tool.`;
  }

  setSessionEnabled(enabled: boolean): void {
    this.#sessionEnabled = enabled;
    this.#activity = true;
    this.#updateStatus();
  }

  async health(): Promise<boolean> {
    const healthy = await this.#worker.checkHealth(true);
    this.#activity = true;
    this.#updateStatus();
    return healthy;
  }

  stop(): void {
    this.#worker.stop();
    try {
      this.#context?.ui.setStatus("headroom", undefined);
    } catch {
      // Host UI failures cannot affect shutdown.
    }
    this.#context = undefined;
  }

  snapshot(): RuntimeSnapshot {
    return {
      enabled: this.#sessionEnabled,
      modelId: this.#modelId,
      tokensBefore: this.#tokensBefore,
      tokensAfter: this.#tokensAfter,
      bytesSaved: this.#bytesSaved,
      retrievals: this.#retrievals,
      lastTransform: { ...this.#lastTransform },
      config: {
        ...this.#config,
        protectedTools: [...this.#config.protectedTools],
      },
      tokensSaved: this.#tokensSaved,
      worker: this.#worker.stats(),
      cache: this.#cache.stats(),
      warnings: [...this.#warnings],
    };
  }

  #modelEligibility(ctx: ExtensionContext): ModelEligibility {
    const model = ctx.model;
    if (!model) return { eligible: true, modelId: "unknown" };

    const modelId = model.provider
      ? `${model.provider}/${model.id}`
      : model.id || "unknown";
    const contextWindow = model.contextWindow;
    return {
      eligible:
        !Number.isFinite(contextWindow) ||
        contextWindow >= this.#config.minContextTokens,
      modelId,
    };
  }

  #onStateChange(state: WorkerState): void {
    this.#updateStatus();
  }

  #updateStatus(): void {
    if (!this.#activity || !this.#context) return;
    try {
      this.#context.ui.setStatus("headroom", formatStatus(this.snapshot()));
    } catch {
      // Host UI failures cannot affect context handling.
    }
  }
}
