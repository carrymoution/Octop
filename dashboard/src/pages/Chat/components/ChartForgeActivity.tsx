import { Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { ToolCallData } from "../hooks/sseHelpers";

/** Deliberately renders only typed metrics, never a raw tool payload. */
export default function ChartForgeActivity({
  toolData,
  isStreaming,
}: {
  toolData: ToolCallData;
  isStreaming: boolean;
}) {
  const { t } = useTranslation();
  let summary: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(toolData.output || "{}");
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed))
      summary = parsed;
  } catch {
    /* Legacy/incomplete summaries still get a safe status label. */
  }
  const state =
    toolData.errorCode || summary.status === "failed"
      ? "failed"
      : isStreaming || summary.status === "running"
      ? "running"
      : "completed";
  const rows = summary.row_count;
  const elapsed = summary.elapsed_ms;
  const queryId = summary.query_id;
  return (
    <Space
      direction="vertical"
      size={4}
      role="status"
      data-testid="chartforge-activity"
    >
      <Typography.Text>{t(`chartforge.display.${state}`)}</Typography.Text>
      <Space size={12} wrap>
        {typeof rows === "number" && Number.isFinite(rows) && rows >= 0 && (
          <Typography.Text type="secondary">
            {t("chartforge.display.rows", { count: rows })}
          </Typography.Text>
        )}
        {typeof elapsed === "number" &&
          Number.isFinite(elapsed) &&
          elapsed >= 0 && (
            <Typography.Text type="secondary">
              {t("chartforge.display.elapsed", {
                seconds: (elapsed / 1000).toFixed(1),
              })}
            </Typography.Text>
          )}
        {summary.truncated === true && (
          <Typography.Text type="secondary">
            {t("chartforge.display.truncated")}
          </Typography.Text>
        )}
      </Space>
      {typeof queryId === "string" &&
        /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(queryId) && (
          <Typography.Text type="secondary">
            {t("chartforge.display.audit", { id: queryId })}
          </Typography.Text>
        )}
    </Space>
  );
}
