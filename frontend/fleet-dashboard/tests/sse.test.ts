import assert from "node:assert/strict";
import test from "node:test";
import { takeSseMessages } from "../src/utils/sse.ts";

test("parses complete SSE event frames and keeps the partial trailing frame", () => {
  const result = takeSseMessages("id: 42\nevent: fleet-event\ndata: {\"driver_id\":\"rider-1\"}\n\nid: 43\ndata: {\"driver_id\":" );
  assert.deepEqual(result.messages, [{ id: "42", event: "fleet-event", data: "{\"driver_id\":\"rider-1\"}" }]);
  assert.equal(result.remainder, "id: 43\ndata: {\"driver_id\":");
});

test("joins multi-line data fields and ignores comments and invalid IDs", () => {
  const result = takeSseMessages(": keep-alive\nid: bad\0id\ndata: first\ndata: second\n\n");
  assert.deepEqual(result.messages, [{ id: undefined, event: undefined, data: "first\nsecond" }]);
  assert.equal(result.remainder, "");
});
