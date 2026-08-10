import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocked = vi.hoisted(() => ({
  compress: vi.fn(),
  start: vi.fn(async () => "http://127.0.0.1:8787"),
  stop: vi.fn(async () => undefined),
  logger: {
    debug: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
  },
}));

vi.mock("headroom-ai", () => ({
  compress: mocked.compress,
}));

vi.mock("../src/proxy-manager.js", () => ({
  ProxyManager: class {
    start = mocked.start;
    stop = mocked.stop;
  },
  defaultLogger: mocked.logger,
}));

import { HeadroomContextEngine } from "../src/engine.js";

afterEach(() => {
  mocked.compress.mockReset();
  mocked.start.mockReset();
  mocked.start.mockResolvedValue("http://127.0.0.1:8787");
  mocked.stop.mockClear();
  mocked.logger.debug.mockClear();
  mocked.logger.error.mockClear();
  mocked.logger.info.mockClear();
  mocked.logger.warn.mockClear();
});

async function createSessionFile(records: unknown[]): Promise<{ directory: string; path: string }> {
  const directory = await mkdtemp(join(tmpdir(), "headroom-openclaw-"));
  const path = join(directory, "session.jsonl");
  await writeFile(path, `${records.map((record) => JSON.stringify(record)).join("\n")}\n`, "utf8");
  return { directory, path };
}

function setProxyUrl(engine: HeadroomContextEngine): void {
  (engine as { proxyUrl: string | null }).proxyUrl = "http://127.0.0.1:8787";
}

describe("HeadroomContextEngine proxy startup helpers", () => {
  it("bootstraps by scheduling proxy startup when enabled", async () => {
    const engine = new HeadroomContextEngine();

    await expect(
      engine.bootstrap({
        sessionId: "session-1",
        sessionFile: "session.jsonl",
      }),
    ).resolves.toEqual({
      bootstrapped: true,
      reason: "proxy startup scheduled",
    });
    expect(mocked.start).toHaveBeenCalledTimes(1);
  });

  it("removes unsubscribed proxy listeners before notifying readiness", async () => {
    const engine = new HeadroomContextEngine();
    const first = vi.fn();
    const second = vi.fn();

    const unsubscribeFirst = engine.onProxyReady(first);
    engine.onProxyReady(second);
    unsubscribeFirst();

    engine.ensureProxyStarted();
    await engine.ensureProxyUrl();

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith("http://127.0.0.1:8787");
  });

  it("returns the existing proxy URL without starting again", async () => {
    const engine = new HeadroomContextEngine();

    (engine as { proxyUrl: string | null }).proxyUrl = "http://127.0.0.1:8787";

    await expect(engine.ensureProxyUrl()).resolves.toBe("http://127.0.0.1:8787");
    expect(mocked.start).not.toHaveBeenCalled();
  });

  it("throws when proxy startup is disabled", async () => {
    const engine = new HeadroomContextEngine({ enabled: false });

    await expect(engine.ensureProxyUrl()).rejects.toThrow("Headroom proxy startup is disabled");
    expect(mocked.start).not.toHaveBeenCalled();
  });

  it("does not emit an unhandledRejection when fire-and-forget startup fails", async () => {
    mocked.start.mockReset();
    mocked.start.mockRejectedValue(new Error("proxy boom"));

    const engine = new HeadroomContextEngine();
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown) => unhandled.push(reason);
    process.on("unhandledRejection", onUnhandled);

    try {
      // Fire-and-forget: caller intentionally does not await.
      engine.ensureProxyStarted();
      // Let the startup promise settle and any microtasks/macrotasks flush.
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(unhandled).toEqual([]);
      expect(mocked.logger.warn).toHaveBeenCalledWith(
        expect.stringContaining("Headroom proxy unavailable"),
      );
    } finally {
      process.off("unhandledRejection", onUnhandled);
    }
  });

  it("stores the startup failure in getProxyStartupError()", async () => {
    const failure = new Error("proxy boom");
    mocked.start.mockReset();
    mocked.start.mockRejectedValue(failure);

    const engine = new HeadroomContextEngine();
    expect(engine.getProxyStartupError()).toBeNull();

    engine.ensureProxyStarted();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(engine.getProxyStartupError()).toBe(failure);
  });

  it("allows retrying startup after a failure", async () => {
    mocked.start.mockReset();
    mocked.start
      .mockRejectedValueOnce(new Error("proxy boom"))
      .mockResolvedValueOnce("http://127.0.0.1:8787");

    const engine = new HeadroomContextEngine();

    engine.ensureProxyStarted();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(engine.getProxyStartupError()).toBeInstanceOf(Error);

    // A second attempt is possible once the failed promise has cleared.
    const url = await engine.ensureProxyUrl();
    expect(url).toBe("http://127.0.0.1:8787");
    expect(engine.getProxyStartupError()).toBeNull();
    expect(mocked.start).toHaveBeenCalledTimes(2);
  });

  it("ensureProxyUrl rejects cleanly on startup failure without unhandledRejection", async () => {
    const failure = new Error("proxy boom");
    mocked.start.mockReset();
    mocked.start.mockRejectedValue(failure);

    const engine = new HeadroomContextEngine();
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown) => unhandled.push(reason);
    process.on("unhandledRejection", onUnhandled);

    try {
      await expect(engine.ensureProxyUrl()).rejects.toBe(failure);
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(unhandled).toEqual([]);
    } finally {
      process.off("unhandledRejection", onUnhandled);
    }
  });

  it("isolates and logs proxy-ready listener rejections", async () => {
    const engine = new HeadroomContextEngine();
    const failing = vi.fn(async () => {
      throw new Error("listener boom");
    });
    const healthy = vi.fn();

    engine.onProxyReady(failing);
    engine.onProxyReady(healthy);

    engine.ensureProxyStarted();
    // ensureProxyUrl must still resolve despite the listener throwing.
    await expect(engine.ensureProxyUrl()).resolves.toBe("http://127.0.0.1:8787");

    expect(failing).toHaveBeenCalled();
    expect(healthy).toHaveBeenCalledWith("http://127.0.0.1:8787");
    expect(mocked.logger.warn).toHaveBeenCalledWith(
      expect.stringContaining("Headroom proxy ready listener failed"),
    );
    expect(engine.getProxyStartupError()).toBeNull();
  });

  it("schedules startup and returns original messages when assembling before proxy readiness", async () => {
    const engine = new HeadroomContextEngine();
    const messages = [{ role: "user", content: "hello" }];

    await expect(
      engine.assemble({
        sessionId: "session-1",
        messages,
      }),
    ).resolves.toEqual({
      messages,
      estimatedTokens: 0,
    });
    expect(mocked.start).toHaveBeenCalledTimes(1);
  });
});

describe("HeadroomContextEngine compaction", () => {
  it("compresses and persists message records without dropping transcript metadata", async () => {
    const session = await createSessionFile([
      { type: "session", version: 1, id: "session-1" },
      { type: "message", message: { role: "user", content: "old ask", timestamp: 1 } },
      {
        type: "model_change",
        provider: "openai-codex",
        model: "gpt-5",
      },
      {
        type: "message",
        message: {
          role: "assistant",
          content: [{ type: "text", text: "old answer" }],
          timestamp: 2,
        },
      },
    ]);
    const engine = new HeadroomContextEngine();
    setProxyUrl(engine);
    mocked.compress.mockResolvedValue({
      messages: [
        { role: "user", content: "compressed ask", _headroomMeta: { timestamp: 1 } },
        { role: "assistant", content: "compressed answer", _headroomMeta: { timestamp: 2 } },
      ],
      tokensBefore: 100,
      tokensAfter: 50,
      tokensSaved: 50,
      compressionRatio: 0.5,
      transformsApplied: ["smart_crusher"],
      ccrHashes: [],
      compressed: true,
    });

    try {
      await expect(
        engine.compact({
          sessionId: "session-1",
          sessionFile: session.path,
          tokenBudget: 60,
          force: true,
        }),
      ).resolves.toEqual({
        ok: true,
        compacted: true,
        reason: "Compacted session with Headroom",
        result: { tokensBefore: 100, tokensAfter: 50 },
      });

      const output = (await readFile(session.path, "utf8"))
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line));
      expect(output[0]).toEqual({ type: "session", version: 1, id: "session-1" });
      expect(output[1]).toEqual({
        type: "message",
        message: { role: "user", content: "compressed ask", timestamp: 1 },
      });
      expect(output[2]).toEqual({
        type: "model_change",
        provider: "openai-codex",
        model: "gpt-5",
      });
      expect(output[3]).toMatchObject({
        type: "message",
        message: {
          role: "assistant",
          content: [{ type: "text", text: "compressed answer" }],
          timestamp: 2,
        },
      });
      expect(mocked.compress).toHaveBeenCalledWith(
        expect.arrayContaining([
          expect.objectContaining({ role: "user", content: "old ask" }),
          expect.objectContaining({ role: "assistant", content: "old answer" }),
        ]),
        {
          model: "claude-sonnet-4-5",
          baseUrl: "http://127.0.0.1:8787",
          fallback: true,
          tokenBudget: 60,
        },
      );
      expect(engine.getStats().compactions).toBe(1);
    } finally {
      await rm(session.directory, { recursive: true, force: true });
    }
  });

  it("fails closed for malformed transcripts without modifying the file", async () => {
    const session = await createSessionFile([{ type: "session", version: 1, id: "session-1" }]);
    await writeFile(session.path, '{"type":"session","version":1,"id":"session-1"}\nnot-json\n', "utf8");
    const original = await readFile(session.path, "utf8");
    const engine = new HeadroomContextEngine();
    setProxyUrl(engine);

    try {
      await expect(
        engine.compact({ sessionId: "session-1", sessionFile: session.path }),
      ).resolves.toMatchObject({
        ok: false,
        compacted: false,
        reason: expect.stringContaining("Invalid JSONL record"),
      });
      expect(await readFile(session.path, "utf8")).toBe(original);
      expect(mocked.compress).not.toHaveBeenCalled();
      expect(engine.getStats().compactions).toBe(0);
    } finally {
      await rm(session.directory, { recursive: true, force: true });
    }
  });

  it("does not rewrite a session when compression reports no change", async () => {
    const session = await createSessionFile([
      { type: "session", version: 1, id: "session-1" },
      { type: "message", message: { role: "user", content: "already compact" } },
    ]);
    const original = await readFile(session.path, "utf8");
    const engine = new HeadroomContextEngine();
    setProxyUrl(engine);
    mocked.compress.mockResolvedValue({
      messages: [{ role: "user", content: "already compact" }],
      tokensBefore: 10,
      tokensAfter: 10,
      tokensSaved: 0,
      compressionRatio: 1,
      transformsApplied: [],
      ccrHashes: [],
      compressed: false,
    });

    try {
      await expect(
        engine.compact({ sessionId: "session-1", sessionFile: session.path }),
      ).resolves.toEqual({
        ok: true,
        compacted: false,
        reason: "Session did not need compression",
        result: { tokensBefore: 10, tokensAfter: 10 },
      });
      expect(await readFile(session.path, "utf8")).toBe(original);
      expect(engine.getStats().compactions).toBe(0);
    } finally {
      await rm(session.directory, { recursive: true, force: true });
    }
  });

  it("does not rewrite a session when compression changes the message count", async () => {
    const session = await createSessionFile([
      { type: "session", version: 1, id: "session-1" },
      { type: "message", message: { role: "user", content: "keep me" } },
    ]);
    const original = await readFile(session.path, "utf8");
    const engine = new HeadroomContextEngine();
    setProxyUrl(engine);
    mocked.compress.mockResolvedValue({
      messages: [],
      tokensBefore: 10,
      tokensAfter: 0,
      tokensSaved: 10,
      compressionRatio: 0,
      transformsApplied: ["history_drop"],
      ccrHashes: [],
      compressed: true,
    });

    try {
      await expect(
        engine.compact({ sessionId: "session-1", sessionFile: session.path }),
      ).resolves.toEqual({
        ok: false,
        compacted: false,
        reason: "Compression changed the session message count",
        result: { tokensBefore: 10, tokensAfter: 0 },
      });
      expect(await readFile(session.path, "utf8")).toBe(original);
      expect(engine.getStats().compactions).toBe(0);
    } finally {
      await rm(session.directory, { recursive: true, force: true });
    }
  });
});
