import {describe, expect, it} from "vitest";
import {ShifdeClient} from "../src/client.js";

const operation = (state = "queued") => ({
  id: "11111111-1111-4111-8111-111111111111", tenant_id: "22222222-2222-4222-8222-222222222222",
  operation_kind: "build", state, created_at: "2026-11-09T12:00:00Z", result: null, error: null,
});

describe("SDK HTTP boundary", () => {
  it("uses a fresh token and preserves an explicit idempotency key", async () => {
    const seen: [string | null, string | null][] = [];
    let tokens = 0;
    const client = new ShifdeClient({baseUrl: "https://delivery.example", tokenProvider: () => `token-${++tokens}`,
      fetch: async (_url, init) => {
        const headers = new Headers(init?.headers);
        seen.push([headers.get("Authorization"), headers.get("Idempotency-Key")]);
        return Response.json(operation(), {status: 202});
      }});
    await client.builds.create({project_id: operation().id}, {idempotencyKey: "request-1"});
    await client.builds.create({project_id: operation().id}, {idempotencyKey: "request-1"});
    expect(seen).toEqual([["Bearer token-1", "request-1"], ["Bearer token-2", "request-1"]]);
  });

  it("rejects malformed operation responses at runtime", async () => {
    const client = new ShifdeClient({baseUrl: "https://delivery.example", tokenProvider: () => "token",
      fetch: async () => Response.json({...operation(), state: "invented-state"})});
    await expect(client.operations.get(operation().id)).rejects.toThrow("Invalid operation response");
  });

  it("surfaces needs_attention without submitting remote cancellation", async () => {
    const methods: string[] = [];
    const client = new ShifdeClient({baseUrl: "https://delivery.example", tokenProvider: () => "token",
      fetch: async (_url, init) => {
        methods.push(init?.method ?? "GET");
        return Response.json(operation("needs_attention"));
      }});
    await expect(client.operations.waitForResult(operation().id, {expectedKind: "build"})).rejects.toThrow("requires attention");
    expect(methods).toEqual(["GET"]);
  });

  it("aborts a hanging request at the polling deadline", async () => {
    const client = new ShifdeClient({baseUrl: "https://delivery.example", tokenProvider: () => "token",
      fetch: async (_url, init) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), {once: true});
      })});
    await expect(client.operations.waitForResult(operation().id, {expectedKind: "build", timeoutMs: 20})).rejects.toThrow();
  });

  it("does not expose an arbitrary server error body", async () => {
    const client = new ShifdeClient({baseUrl: "https://delivery.example", tokenProvider: () => "token",
      fetch: async () => new Response("secret-do-not-expose", {status: 500})});
    await expect(client.builds.create({project_id: operation().id})).rejects.toThrow("HTTP 500");
  });

  it("rejects insecure or credential-bearing API origins", () => {
    for (const baseUrl of ["http://delivery.example", "https://user:password@delivery.example", "https://delivery.example/api"]) {
      expect(() => new ShifdeClient({baseUrl, tokenProvider: () => "token"})).toThrow();
    }
  });

  it("rejects malformed submissions before sending a request", async () => {
    let called = false;
    const client = new ShifdeClient({baseUrl: "https://delivery.example", tokenProvider: () => "token",
      fetch: async () => { called = true; return Response.json(operation()); }});
    await expect(client.builds.create({project_id: "not-a-uuid"})).rejects.toThrow("Invalid submission");
    expect(called).toBe(false);
  });
});
