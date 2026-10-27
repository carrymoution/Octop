import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import ConnectorsPage from "./index";
import { chartforgeApi } from "../../../api/modules/chartforge";

const state = vi.hoisted(() => ({
  user: {
    id: 7,
    role: "user",
    permissions: ["connector_enabled", "connector_builtin", "connector_custom"],
  },
}));

vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => state.user,
}));
vi.mock("./useConnectors", () => ({
  useConnectorInstances: () => ({
    catalog: [{ kind: "chartforge", name: "AI报表查询" }],
    instances: [],
    loading: false,
    refresh: vi.fn(),
  }),
}));
vi.mock("../../../api/modules/chartforge", () => ({
  chartforgeApi: { status: vi.fn() },
}));
vi.mock("./CustomMcpTab", () => ({
  CustomMcpTab: () => <div>Custom MCP</div>,
}));
vi.mock("./ConnectorCard", () => ({
  ConnectorCard: () => <div>Generic connector card</div>,
}));

describe("personal report connector after upstream integration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.user.permissions = [
      "connector_enabled",
      "connector_builtin",
      "connector_custom",
    ];
    vi.mocked(chartforgeApi.status).mockResolvedValue({
      base_url: "http://reports.example",
      enabled: true,
      allow_http: true,
      connected: true,
      employee_no: "0007",
    });
  });

  function renderPage() {
    return render(
      <MemoryRouter>
        <ConnectorsPage />
      </MemoryRouter>,
    );
  }

  it("counts a connected personal report grant without showing the empty guide", async () => {
    renderPage();
    expect(
      await screen.findByText("已启用 1 个连接器实例"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("connectors.emptyGuideTitle"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("resourceAudience.empty"),
    ).not.toBeInTheDocument();
  });

  it("hides the personal grant in shared resources and restores it for local resources", async () => {
    renderPage();
    await screen.findByText("chartforge.title");
    await screen.findByText("已启用 1 个连接器实例");
    fireEvent.click(screen.getByText("resourceAudience.shared"));
    expect(screen.queryByText("chartforge.title")).not.toBeInTheDocument();
    expect(screen.getByText("已启用 0 个连接器实例")).toBeInTheDocument();
    expect(screen.getByText("resourceAudience.empty")).toBeInTheDocument();
    fireEvent.click(screen.getByText("resourceAudience.local"));
    expect(await screen.findByText("chartforge.title")).toBeInTheDocument();
  });

  it("shows the report connector only once in the builtin catalog", async () => {
    renderPage();
    await screen.findByText("已启用 1 个连接器实例");
    fireEvent.click(screen.getByRole("tab", { name: "connectors.tabBuiltin" }));
    expect(screen.getAllByText("chartforge.title")).toHaveLength(1);
    expect(
      screen.queryByText("Generic connector card"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("当前支持 1 个连接器，已配置 1 个"),
    ).toBeInTheDocument();
  });

  it("respects a user who can only open the custom MCP tab", () => {
    state.user.permissions = ["connector_custom"];
    renderPage();
    expect(screen.getByText("Custom MCP")).toBeInTheDocument();
    expect(screen.queryByText("chartforge.title")).not.toBeInTheDocument();
    expect(chartforgeApi.status).not.toHaveBeenCalled();
  });
});
