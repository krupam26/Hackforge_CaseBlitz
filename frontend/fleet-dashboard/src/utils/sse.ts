export type SseMessage = { id?: string; event?: string; data: string };

export function takeSseMessages(buffer: string): { messages: SseMessage[]; remainder: string } {
  const frames = buffer.split(/\r?\n\r?\n/);
  const remainder = frames.pop() || "";
  const messages = frames.flatMap((frame) => {
    let id: string | undefined;
    let event: string | undefined;
    const data: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "id" && !value.includes("\0")) id = value;
      if (field === "event") event = value;
      if (field === "data") data.push(value);
    }
    return data.length ? [{ id, event, data: data.join("\n") }] : [];
  });
  return { messages, remainder };
}
