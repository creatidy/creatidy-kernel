// SPDX-License-Identifier: Apache-2.0
// Test instrumentation, NOT an OS/network sandbox or hostile-worker boundary.
const nativeFetch = globalThis.fetch
globalThis.fetch = (input, options) => {
  const url = new URL(input instanceof Request ? input.url : String(input))
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1") {
    throw new Error("offline fixture refused non-loopback fetch")
  }
  return nativeFetch(input, options)
}
