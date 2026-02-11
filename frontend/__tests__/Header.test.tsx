import { render, screen } from "@testing-library/react";
import Header from "@/components/Header";

describe("Header", () => {
  it("renders portfolio value and cash balance", () => {
    render(
      <Header totalValue={12345.67} cashBalance={5000} status="connected" />
    );
    expect(screen.getByText("$12,345.67")).toBeInTheDocument();
    expect(screen.getByText("$5,000.00")).toBeInTheDocument();
  });

  it("renders FinAlly branding", () => {
    render(
      <Header totalValue={10000} cashBalance={10000} status="connected" />
    );
    expect(screen.getByText("FinAlly")).toBeInTheDocument();
  });

  it("shows Live when connected", () => {
    render(
      <Header totalValue={10000} cashBalance={10000} status="connected" />
    );
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("shows Connecting... when connecting", () => {
    render(
      <Header totalValue={10000} cashBalance={10000} status="connecting" />
    );
    expect(screen.getByText("Connecting...")).toBeInTheDocument();
  });

  it("shows Disconnected when disconnected", () => {
    render(
      <Header totalValue={10000} cashBalance={10000} status="disconnected" />
    );
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
  });
});
