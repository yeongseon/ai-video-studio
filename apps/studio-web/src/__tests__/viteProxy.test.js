// @vitest-environment node
import { once } from "node:events";
import { createServer as createHttpServer } from "node:http";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { createServer } from "vite";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

describe.each(["", "synthetic-server-creator-key"])(
  "Vite proxy with VITE_API_KEY=%j",
  (apiKey) => {
    const received = new Map();
    const upstream = createHttpServer((request, response) => {
      received.set(request.url ?? "", request.headers);
      response.end("upstream response");
    });
    let vite;
    let origin;
    let prefixes;

    beforeAll(async () => {
      upstream.listen(0, "127.0.0.1");
      await once(upstream, "listening");
      const address = upstream.address();
      if (address === null || typeof address === "string") {
        throw new TypeError("Expected an upstream TCP address");
      }
      vi.stubEnv("API_TARGET", `http://127.0.0.1:${address.port}`);
      vi.stubEnv("VITE_API_KEY", apiKey);
      const root = fileURLToPath(new URL("../../", import.meta.url));
      vite = await createServer({
        root,
        configFile: `${root}vite.config.ts`,
        server: {
          host: "127.0.0.1",
          port: 0,
          strictPort: false,
          hmr: false,
          watch: null,
        },
        optimizeDeps: { noDiscovery: true, include: [] },
        plugins: [{
          name: "exercise-every-proxy-prefix",
          configResolved(config) {
            // Exercise specific entries before shorter prefixes can shadow them.
            config.server.proxy = Object.fromEntries(
              Object.entries(config.server.proxy ?? {}).sort(
                ([left], [right]) => right.length - left.length,
              ),
            );
          },
        }],
      });
      prefixes = Object.keys(vite.config.server.proxy ?? {});
      const httpServer = vite.httpServer;
      if (httpServer === null) {
        throw new TypeError("Expected a Vite HTTP server");
      }
      httpServer.listen(0, "127.0.0.1");
      await once(httpServer, "listening");
      const viteAddress = httpServer.address();
      if (viteAddress == null || typeof viteAddress === "string") {
        throw new TypeError("Expected a Vite TCP address");
      }
      origin = `http://127.0.0.1:${viteAddress.port}`;
    });

    afterAll(async () => {
      try {
        await vite?.close();
      } finally {
        try {
          await promisify(upstream.close.bind(upstream))();
        } finally {
          vi.unstubAllEnvs();
        }
      }
    });

    it.each(["X-Admin-Key", "x-admin-key", "X-aDmIn-KeY"])(
      "drops client %s on every prefix while preserving other headers",
      async (adminHeader) => {
        // Given every prefix in the real configuration and a client admin key.
        expect(prefixes.length).toBeGreaterThan(0);
        for (const prefix of prefixes) {
          // When the request passes through the real Vite proxy to the upstream.
          const response = await fetch(`${origin}${prefix}`, {
            headers: {
              [adminHeader]: "synthetic-client-admin-key",
              "X-API-Key": "synthetic-client-creator-key",
              "X-Test": "preserved",
            },
            signal: AbortSignal.timeout(2000),
          });

          // Then only the admin credential is removed; creator policy is unchanged.
          expect(await response.text()).toBe("upstream response");
          expect(received.get(prefix)?.["x-test"], prefix).toBe("preserved");
          expect(received.get(prefix)?.["x-api-key"], prefix).toBe(
            apiKey || "synthetic-client-creator-key",
          );
          expect.soft(received.get(prefix), prefix).not.toHaveProperty("x-admin-key");
        }
      },
    );
  },
);
