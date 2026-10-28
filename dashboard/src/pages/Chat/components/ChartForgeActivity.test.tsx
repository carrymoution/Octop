import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ChartForgeActivity from "./ChartForgeActivity";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

describe("report activity summary", () => {
  it("only renders status and typed metrics, even if a legacy payload includes SQL", () => {
    const { container } = render(
      <ChartForgeActivity
        isStreaming={false}
        toolData={{
          detailsRedacted: true,
          arguments: "SELECT SECRET_FIELD FROM ADS.PRIVATE_TABLE",
          output: JSON.stringify({
            status: "completed",
            row_count: 3,
            elapsed_ms: 12,
            sql: "PRIVATE_TABLE",
            artifact: "PRIVATE_TABLE",
            query_id: "PRIVATE_TABLE",
          }),
        }}
      />,
    );
    expect(
      screen.getByText("chartforge.display.completed"),
    ).toBeInTheDocument();
    expect(screen.getByText("chartforge.display.rows")).toBeInTheDocument();
    expect(container.textContent).not.toContain("PRIVATE_TABLE");
    expect(container.querySelector("button, textarea, pre")).toBeNull();
  });
  it("shows running and failed states without raw exceptions", () => {
    const { rerender, container } = render(
      <ChartForgeActivity isStreaming toolData={{ detailsRedacted: true }} />,
    );
    expect(screen.getByText("chartforge.display.running")).toBeInTheDocument();
    rerender(
      <ChartForgeActivity
        isStreaming={false}
        toolData={{
          detailsRedacted: true,
          errorCode: "tool_error",
          output: "ORA-00942 ADS.PRIVATE_TABLE",
        }}
      />,
    );
    expect(screen.getByText("chartforge.display.failed")).toBeInTheDocument();
    expect(container.textContent).not.toContain("ORA-");
  });
});
