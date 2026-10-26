// SPDX-License-Identifier: Apache-2.0
// Run only in the pinned disposable Kilo source export, at test/kernel52/.
// Layer/setup idioms adapted from Kilo's MIT test fixtures; see NOTICE.
import { expect, test } from "bun:test"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { provideTestInstance, tmpdir } from "../fixture/fixture"
import { Server } from "../../src/server/server"

const PIN = "sr-pin:kernel-fixture/openai/gpt-fixture/opaque-config"
const REQUIREMENTS = {
  schema_version: 1,
  requirement: {
    task_level: "L0",
    capability_minima: {},
    hard_constraints: {},
  },
  binding: {
    pinned_target: {
      resource_id: "kernel-fixture",
      model: { provider: "openai", model: "gpt-fixture", variant: "opaque-config" },
    },
    maximum_output_tokens: 1024,
    requires_streaming: true,
  },
  profile_policy_version: null,
  profile_expansion: null,
  reasoning_effort: "low",
}

function response(model: string) {
  const chunk = (delta: object, finish_reason: string | null = null) =>
    "data: " + JSON.stringify({
      id: "synthetic-completion",
      object: "chat.completion.chunk",
      created: 1,
      model,
      choices: [{ index: 0, delta, finish_reason }],
    }) + "\n\n"
  return new Response(
    chunk({ role: "assistant", content: "synthetic offline answer" }) +
    chunk({}, "stop") + "data: [DONE]\n\n",
    { headers: { "Content-Type": "text/event-stream" } },
  )
}

// This fake records raw serialized input. It is NOT the real Router validator.
// Acceptance by this fake is transport evidence only, never D079 conformance.
const modes = process.env.KERNEL52_EXPECT_BYPASS
  ? ["bypassed"]
  : ["capture", "guarded", "absent", "missing", "init-throws", "config-throws", "guard-throws", "publish-then-throw"]
for (const mode of modes) {
  test(`real Kilo provider dispatch: ${mode}`, async () => {
    const requests: object[] = []
    const router = Bun.serve({
      hostname: "127.0.0.1",
      port: 0,
      fetch: async (request) => {
        const body = await request.json()
        requests.push(body)
        if (process.env.KERNEL52_RECEIPTS) {
          await Bun.write(path.join(process.env.KERNEL52_RECEIPTS, mode + ".body.json"), JSON.stringify(body))
        }
        return response(PIN)
      },
    })
    const endpoint = `http://127.0.0.1:${router.port}/v1`
    const plugin = pathToFileURL(path.join(import.meta.dir, "guard.mjs")).href
    try {
      await using tmp = await tmpdir({
        config: {
          model: `kernel/${PIN}`,
          small_model: `kernel/${PIN}`,
          enabled_providers: ["kernel"],
          plugin: mode === "absent" ? [] : [[mode === "missing" ? plugin + ".missing" : plugin, {
            endpoint, pin: PIN, requirements: REQUIREMENTS, mode,
          }]],
          agent: { title: { disable: true } },
          compaction: { auto: false },
          permission: { "*": "deny" },
        },
      })
      await provideTestInstance({
        directory: tmp.path,
        fn: async () => {
          const app = Server.Default().app
          const headers = { "Content-Type": "application/json", "x-kilo-directory": tmp.path }
          const created = await app.request("/session", { method: "POST", headers, body: "{}" })
          expect(created.status).toBe(200)
          const { id } = await created.json()
          const result = await app.request(`/session/${id}/message`, {
            method: "POST", headers,
            body: JSON.stringify({ parts: [{ type: "text", text: "Return one synthetic answer." }] }),
          })
          const text = await result.text()
          if (mode === "capture" || mode === "publish-then-throw") {
            expect(requests.length, text).toBe(1)
            const body = requests[0] as Record<string, any>
            expect(body.model).toBe(PIN)
            expect(body.execution_requirements).toEqual(REQUIREMENTS)
            expect(body.execution_requirements.profile_expansion).toBeNull()
            expect(body.execution_requirements.profile_policy_version).toBeNull()
            const users = body.messages.filter((m: any) => m.role === "user")
            expect(users).toHaveLength(1)
            expect(users[0].content).toHaveLength(2)
            expect(users[0].content[0]).toEqual({ type: "text", text: "Return one synthetic answer." })
            expect(users[0].content[1].text).toContain("<environment_details>")
            expect(text).toContain("synthetic offline answer")
          } else {
            expect(requests.length).toBe(0)
            expect(text).not.toContain("synthetic offline answer")
            if (mode === "guarded") expect(text).toContain("refuses multipart content")
          }
          if (process.env.KERNEL52_RECEIPTS) {
            await Bun.write(path.join(process.env.KERNEL52_RECEIPTS, mode + ".result.json"), JSON.stringify({
              mode, requests: requests.length, status: result.status,
              synthetic_answer: text.includes("synthetic offline answer"),
            }))
          }
        },
      })
    } finally {
      await router.stop(true)
    }
  }, 60000)
}
