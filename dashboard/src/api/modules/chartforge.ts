import { request } from "../request";
export interface ChartForgeSettings {
  base_url: string;
  frontend_url?: string;
  enabled: boolean;
  allow_http: boolean;
  connected: boolean;
  needs_reauthorization?: boolean;
  employee_no?: string;
}
export const chartforgeApi = {
  status: () => request<ChartForgeSettings>("/connectors/chartforge"),
  check: (data: ChartForgeSettings) =>
    request<{ base_url: string }>("/connectors/chartforge/check", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  authorize: () =>
    request<{ authorize_url: string }>("/connectors/chartforge/authorize", {
      method: "POST",
    }),
  disconnect: () => request("/connectors/chartforge", { method: "DELETE" }),
  save: (data: ChartForgeSettings) =>
    request("/connectors/chartforge/settings", {
      method: "PUT",
      body: JSON.stringify(data),
    }),
};
