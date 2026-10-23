import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChartForgeConnector } from "./ChartForgeConnector";
import { chartforgeApi } from "../../../api/modules/chartforge";

vi.mock("../../../api/modules/chartforge", () => ({
  chartforgeApi: {
    status: vi.fn(),
    authorize: vi.fn(),
    disconnect: vi.fn(),
    save: vi.fn(),
    check: vi.fn(),
  },
}));
vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ role: "admin" }),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

describe("ChartForgeConnector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    const computedStyle = window.getComputedStyle;
    vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
      computedStyle(element),
    );
  });
  afterEach(() => vi.restoreAllMocks());

  it("requires administrator setup before employees can authorize", async () => {
    vi.mocked(chartforgeApi.status).mockResolvedValue({
      base_url: "",
      enabled: false,
      allow_http: false,
      connected: false,
    });
    render(<ChartForgeConnector />);
    await screen.findByText("chartforge.disabled");
    expect(
      screen.getByRole("button", { name: "chartforge.connect" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "chartforge.settings" }),
    ).toBeEnabled();
  });

  it("links employees to the platform frontend and keeps the backend out of the card", async () => {
    vi.mocked(chartforgeApi.status).mockResolvedValue({
      base_url: "http://localhost:8080",
      frontend_url: "http://localhost",
      enabled: true,
      allow_http: true,
      connected: true,
      employee_no: "10004841",
    });
    render(<ChartForgeConnector />);
    const link = await screen.findByRole("link", { name: "http://localhost" });
    expect(link).toHaveAttribute("href", "http://localhost");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByText("http://localhost:8080")).toBeNull();
  });

  it("navigates to authorization in the same tab without an empty popup", async () => {
    vi.mocked(chartforgeApi.status).mockResolvedValue({
      base_url: "http://internal",
      enabled: true,
      allow_http: true,
      connected: false,
    });
    vi.mocked(chartforgeApi.authorize).mockResolvedValue({
      authorize_url: "http://internal/oauth2/authorize?state=test",
    });
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<ChartForgeConnector />);
    await screen.findByText("chartforge.ready");
    fireEvent.click(screen.getByRole("button", { name: "chartforge.connect" }));
    await waitFor(() =>
      expect(open).toHaveBeenCalledWith(
        "http://internal/oauth2/authorize?state=test",
        "_self",
      ),
    );
    expect(chartforgeApi.authorize).toHaveBeenCalledTimes(1);
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it("keeps actionable authorization errors visible without opening a blank window", async () => {
    vi.mocked(chartforgeApi.status).mockResolvedValue({
      base_url: "http://frontend",
      enabled: true,
      allow_http: true,
      connected: false,
    });
    vi.mocked(chartforgeApi.authorize).mockRejectedValue(
      new Error(
        JSON.stringify({
          error: {
            code: "CONNECTOR_INVALID_CREDENTIALS",
            details: { reason_key: "chartforge.invalidServer" },
          },
        }),
      ),
    );
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<ChartForgeConnector />);
    await screen.findByText("chartforge.ready");
    fireEvent.click(screen.getByRole("button", { name: "chartforge.connect" }));
    await screen.findByText("chartforge.invalidServer");
    expect(open).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "chartforge.connect" }),
    ).toBeEnabled();
  });

  it("shows a retry action when status loading fails instead of treating it as disabled", async () => {
    vi.mocked(chartforgeApi.status).mockRejectedValueOnce(
      new Error("network failed"),
    );
    render(<ChartForgeConnector />);
    await screen.findByText("chartforge.loadFailed");
    expect(screen.queryByText("chartforge.disabled")).toBeNull();
    vi.mocked(chartforgeApi.status).mockResolvedValue({
      base_url: "http://internal",
      enabled: true,
      allow_http: true,
      connected: false,
    });
    fireEvent.click(screen.getByRole("button", { name: "chartforge.retry" }));
    await waitFor(() =>
      expect(screen.queryByText("chartforge.loadFailed")).toBeNull(),
    );
    expect(
      screen.getByRole("button", { name: "chartforge.connect" }),
    ).toBeEnabled();
  });

  it("validates a backend origin before saving and leaves field errors in the form", async () => {
    vi.mocked(chartforgeApi.status).mockResolvedValue({
      base_url: "http://internal",
      enabled: true,
      allow_http: true,
      connected: false,
    });
    render(<ChartForgeConnector />);
    await screen.findByText("chartforge.ready");
    fireEvent.click(
      screen.getByRole("button", { name: "chartforge.settings" }),
    );
    fireEvent.change(
      await screen.findByPlaceholderText("http://localhost:8080"),
      { target: { value: "http://localhost:8080/sql-gateway" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "chartforge.saveAndCheck" }),
    );
    await screen.findByText("chartforge.invalidOrigin");
    expect(chartforgeApi.save).not.toHaveBeenCalled();
  });

  it("can check the platform before saving organization settings", async () => {
    vi.mocked(chartforgeApi.status).mockResolvedValue({
      base_url: "http://internal",
      enabled: true,
      allow_http: true,
      connected: false,
    });
    vi.mocked(chartforgeApi.check).mockResolvedValue({
      base_url: "http://internal",
    });
    render(<ChartForgeConnector />);
    await screen.findByText("chartforge.ready");
    fireEvent.click(
      screen.getByRole("button", { name: "chartforge.settings" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "chartforge.check" }),
    );
    await screen.findByText("chartforge.checkPassed");
    expect(chartforgeApi.check).toHaveBeenCalledTimes(1);
    expect(chartforgeApi.save).not.toHaveBeenCalled();
  });
});
