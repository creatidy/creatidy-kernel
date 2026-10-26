// SPDX-License-Identifier: Apache-2.0
// Offline experiment only. Not a product plugin or a durable admission implementation.
import assert from "node:assert/strict"

export default async function (_input, options) {
  if (options.mode === "init-throws") throw new Error("synthetic initialization refusal")
  return {
    config: async (config) => {
      if (options.mode === "config-throws") throw new Error("synthetic configuration refusal")
      const guard = async (input, init) => {
        if (options.mode === "guard-throws") throw new Error("synthetic dispatch refusal")
        assert.equal(String(input), options.endpoint + "/chat/completions")
        const body = JSON.parse(init.body)
        assert.equal(body.model, options.pin)
        assert.deepEqual(body.execution_requirements, options.requirements)
        assert.equal(body.reasoning_effort, "low")
        assert.equal(body.max_tokens, 1024)
        if (options.mode !== "capture") {
          assert.ok(body.messages.every((message) =>
            typeof message.content === "string" || message.content === null),
            "Router Chat surface 1 refuses multipart content")
        }
        return fetch(input, init)
      }
      const provider = {
        npm: "@ai-sdk/openai-compatible",
        name: "Kernel synthetic proof",
        options: { baseURL: options.endpoint, fetch: guard },
        models: {
          [options.pin]: {
            name: "Synthetic route",
            tool_call: true,
            reasoning: true,
            limit: { context: 128000, output: 1024 },
            options: { execution_requirements: options.requirements, reasoningEffort: "low" },
            cost: { input: 0, output: 0 },
          },
        },
      }
      // Publish last: upstream swallows hook errors without rolling back mutations.
      config.provider = { kernel: provider }
      if (options.mode === "publish-then-throw") {
        // Deliberately unsafe counterexample, exercised only against the fake.
        delete provider.options.fetch
        throw new Error("synthetic failure after unsafe publication")
      }
    },
  }
}
