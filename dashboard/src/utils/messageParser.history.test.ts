import { describe, expect, it } from "vitest";
import {
  extractText,
  extractToolData,
  filterDialogueHistoryMessages,
  formatHistoryMessageText,
} from "./messageParser";

describe("formatHistoryMessageText", () => {
  it("preserves redacted report calls and results across history reload", () => {
    for (const block of [
      {
        type: "tool_use",
        name: "chartforge_query_sql",
        id: "c",
        input: {},
        details_redacted: true,
      },
      {
        type: "tool_result",
        name: "chartforge_query_sql",
        id: "c",
        output: "Query completed",
        details_redacted: true,
      },
    ]) {
      expect(extractToolData([block])?.data.detailsRedacted).toBe(true);
    }
  });
  it("returns tool output when text blocks are empty", () => {
    const content = [
      { type: "tool_result", output: "grep result", tool_use_id: "call_1" },
    ];
    expect(extractText(content).trim()).toBe("");
    expect(formatHistoryMessageText(content)).toBe("grep result");
  });
});

describe("filterDialogueHistoryMessages", () => {
  it("keeps user and assistant text, drops tool role and tool blocks", () => {
    const messages = [
      { role: "user", content: [{ type: "text", text: "hello" }] },
      {
        role: "assistant",
        content: [
          { type: "text", text: "answer" },
          { type: "tool_use", name: "grep", input: { q: "x" } },
        ],
      },
      {
        role: "tool",
        content: [{ type: "tool_result", output: "grep result" }],
      },
      { role: "system", content: [{ type: "text", text: "system prompt" }] },
      {
        role: "assistant",
        content: [{ type: "tool_use", name: "only_tool", input: {} }],
      },
    ];

    const filtered = filterDialogueHistoryMessages(messages);
    expect(filtered).toHaveLength(2);
    expect(filtered[0].role).toBe("user");
    expect(extractText(filtered[1].content)).toBe("answer");
    expect(
      (filtered[1].content as Array<{ type: string }>).some(
        (b) => b.type === "tool_use",
      ),
    ).toBe(false);
  });
});
