import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Modal,
  Space,
  Switch,
  Tag,
  Typography,
} from "antd";
import { Database } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  chartforgeApi,
  type ChartForgeSettings as Settings,
} from "../../../api/modules/chartforge";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import { apiErrorMessage, parseApiError } from "../../../utils/apiError";
import { notifyConnectorsChanged } from "./customMcpUtils";
import styles from "./index.module.less";

type PendingAction = "connect" | "disconnect" | "check" | "save" | null;

export function ChartForgeConnector({
  onAvailabilityChange,
}: {
  onAvailabilityChange?: (status: {
    enabled: boolean;
    connected: boolean;
  }) => void;
}) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const [status, setStatus] = useState<Settings>();
  const [editing, setEditing] = useState(false);
  const [pending, setPending] = useState<PendingAction>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [actionError, setActionError] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [checked, setChecked] = useState(false);
  const [form] = Form.useForm<Settings>();
  const address = Form.useWatch("base_url", form) as string | undefined;
  const admin = user?.role === "admin";
  const callback = window.location.origin + "/api/connectors/oauth/callback";
  const refresh = useCallback(async () => {
    try {
      const value = await chartforgeApi.status();
      setStatus(value);
      onAvailabilityChange?.({
        enabled: value.enabled,
        connected: value.connected,
      });
      setLoadError(false);
    } catch {
      setLoadError(true);
    } finally {
      setInitialLoading(false);
    }
  }, [onAvailabilityChange]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (
        event.origin === window.location.origin &&
        event.data?.type === "octop:chartforge-oauth"
      ) {
        void refresh();
        notifyConnectorsChanged();
      }
    };
    window.addEventListener("message", receive);
    window.addEventListener("focus", refresh);
    return () => {
      window.removeEventListener("message", receive);
      window.removeEventListener("focus", refresh);
    };
  }, [refresh]);
  const perform = async (
    kind: Exclude<PendingAction, null>,
    action: () => Promise<void>,
  ) => {
    const settingsAction = kind === "check" || kind === "save";
    const setError = settingsAction ? setSettingsError : setActionError;
    setPending(kind);
    setError("");
    try {
      await action();
    } catch (error) {
      const parsed = parseApiError(error);
      const key = parsed?.details?.reason_key;
      setError(
        typeof key === "string" && key.startsWith("chartforge.")
          ? t(key, parsed?.details ?? {})
          : apiErrorMessage(error, t("chartforge.failed"), t),
      );
    } finally {
      setPending(null);
    }
  };
  const editSettings = () => {
    if (status) form.setFieldsValue(status);
    setChecked(false);
    setSettingsError("");
    setEditing(true);
  };
  return (
    <Card size="small" className={styles.chartforgeCard}>
      <div className={styles.chartforgeHeader}>
        <Space>
          <Database size={22} aria-hidden />
          <Typography.Text strong>{t("chartforge.title")}</Typography.Text>
          {!initialLoading && !loadError && (
            <Tag color={status?.connected ? "success" : "default"}>
              {t(
                status?.connected
                  ? "chartforge.connectedStatus"
                  : "chartforge.notConnected",
              )}
            </Tag>
          )}
        </Space>
        {admin && (
          <Button
            type="text"
            disabled={initialLoading || loadError || !!pending}
            onClick={editSettings}
          >
            {t("chartforge.settings")}
          </Button>
        )}
      </div>
      <Typography.Paragraph
        type="secondary"
        className={styles.chartforgeDescription}
      >
        {t("chartforge.description")}
      </Typography.Paragraph>
      {loadError ? (
        <Alert
          type="error"
          showIcon
          message={t("chartforge.loadFailed")}
          action={
            <Button size="small" onClick={() => void refresh()}>
              {t("chartforge.retry")}
            </Button>
          }
        />
      ) : initialLoading ? (
        <Typography.Text type="secondary">
          {t("chartforge.loading")}
        </Typography.Text>
      ) : (
        <>
          {(status?.frontend_url || user?.username) && (
            <div className={styles.chartforgeDetails}>
              {status?.frontend_url && (
                <span>
                  {t("chartforge.platform")}{" "}
                  <Typography.Link
                    href={status.frontend_url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {status.frontend_url}
                  </Typography.Link>
                </span>
              )}
              <span>
                {t("chartforge.employee")} <strong>{user?.username}</strong>
              </span>
            </div>
          )}
          <Typography.Paragraph className={styles.chartforgeHint}>
            {status?.connected
              ? t("chartforge.bound", { employee: status.employee_no })
              : t(
                  status?.needs_reauthorization
                    ? "chartforge.authorizationExpired"
                    : status?.enabled
                    ? "chartforge.ready"
                    : "chartforge.disabled",
                )}
          </Typography.Paragraph>
        </>
      )}
      {actionError && (
        <Alert
          className={styles.chartforgeError}
          type="error"
          showIcon
          message={actionError}
        />
      )}
      <Space wrap>
        <Button
          type="primary"
          loading={pending === "connect"}
          disabled={
            initialLoading ||
            loadError ||
            !status?.enabled ||
            (!!pending && pending !== "connect")
          }
          onClick={() =>
            void perform("connect", async () => {
              const result = await chartforgeApi.authorize();
              window.open(result.authorize_url, "_self");
            })
          }
        >
          {t(
            pending === "connect"
              ? "chartforge.connecting"
              : status?.connected
              ? "chartforge.reconnect"
              : "chartforge.connect",
          )}
        </Button>
        {status?.connected && (
          <Button
            loading={pending === "disconnect"}
            disabled={!!pending && pending !== "disconnect"}
            onClick={() =>
              Modal.confirm({
                title: t("chartforge.disconnectConfirm"),
                content: t("chartforge.disconnectHint"),
                onOk: () =>
                  perform("disconnect", async () => {
                    await chartforgeApi.disconnect();
                    await refresh();
                    notifyConnectorsChanged();
                  }),
              })
            }
          >
            {t("chartforge.disconnect")}
          </Button>
        )}
      </Space>
      <Modal
        open={editing}
        title={t("chartforge.settings")}
        onCancel={() => setEditing(false)}
        confirmLoading={pending === "save"}
        okText={t("chartforge.saveAndCheck")}
        okButtonProps={{ disabled: !!pending && pending !== "save" }}
        cancelButtonProps={{ disabled: !!pending }}
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          onValuesChange={() => {
            setChecked(false);
            setSettingsError("");
          }}
          onFinish={(values) =>
            void perform("save", async () => {
              await chartforgeApi.save(values);
              setEditing(false);
              setActionError("");
              await refresh();
            })
          }
        >
          <Form.Item
            name="base_url"
            label={t("chartforge.origin")}
            extra={t("chartforge.originHelp")}
            rules={[
              { required: true, message: t("chartforge.addressRequired") },
              {
                validator: async (_, value: string) => {
                  if (!value) return;
                  try {
                    const url = new URL(value.trim());
                    if (
                      !["http:", "https:"].includes(url.protocol) ||
                      url.username ||
                      url.password ||
                      (url.pathname !== "/" && url.pathname !== "") ||
                      url.search ||
                      url.hash
                    )
                      throw new Error();
                  } catch {
                    throw new Error(t("chartforge.invalidOrigin"));
                  }
                },
              },
            ]}
          >
            <Input placeholder="http://localhost:8080" disabled={!!pending} />
          </Form.Item>
          {address?.trim().startsWith("http:") && (
            <Form.Item
              name="allow_http"
              valuePropName="checked"
              extra={t("chartforge.httpHelp")}
            >
              <Checkbox disabled={!!pending}>
                {t("chartforge.allowHttp")}
              </Checkbox>
            </Form.Item>
          )}
          <Form.Item
            name="enabled"
            label={t("chartforge.enable")}
            valuePropName="checked"
          >
            <Switch disabled={!!pending} />
          </Form.Item>
          <div className={styles.chartforgeCallback}>
            <Typography.Text strong>
              {t("chartforge.callbackLabel")}
            </Typography.Text>
            <Typography.Paragraph type="secondary">
              {t("chartforge.callbackHelp")}
            </Typography.Paragraph>
            <Typography.Paragraph code copyable={{ text: callback }}>
              {callback}
            </Typography.Paragraph>
          </div>
          {settingsError && (
            <Alert
              type="error"
              showIcon
              message={settingsError}
              className={styles.chartforgeError}
            />
          )}
          {checked && (
            <Alert
              type="success"
              showIcon
              message={t("chartforge.checkPassed")}
              className={styles.chartforgeError}
            />
          )}
          <Button
            loading={pending === "check"}
            disabled={!!pending && pending !== "check"}
            onClick={() =>
              void form
                .validateFields(["base_url"])
                .then((values) =>
                  perform("check", async () => {
                    await chartforgeApi.check({
                      ...form.getFieldsValue(),
                      ...values,
                    });
                    setChecked(true);
                  }),
                )
                .catch(() => {})
            }
          >
            {t("chartforge.check")}
          </Button>
        </Form>
      </Modal>
    </Card>
  );
}
