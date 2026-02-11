import { render } from "@testing-library/react";
import Sparkline from "@/components/Sparkline";

describe("Sparkline", () => {
  it("renders nothing for empty data", () => {
    const { container } = render(<Sparkline data={[]} />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders nothing for single data point", () => {
    const { container } = render(<Sparkline data={[100]} />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders SVG for valid data", () => {
    const { container } = render(<Sparkline data={[100, 105, 103]} />);
    const svg = container.querySelector("svg");
    expect(svg).toBeInTheDocument();
  });

  it("renders polyline element", () => {
    const { container } = render(<Sparkline data={[100, 105, 103]} />);
    const polyline = container.querySelector("polyline");
    expect(polyline).toBeInTheDocument();
  });

  it("uses green color when price went up", () => {
    const { container } = render(<Sparkline data={[100, 110]} />);
    const polyline = container.querySelector("polyline");
    expect(polyline?.getAttribute("stroke")).toBe("#26a641");
  });

  it("uses red color when price went down", () => {
    const { container } = render(<Sparkline data={[110, 100]} />);
    const polyline = container.querySelector("polyline");
    expect(polyline?.getAttribute("stroke")).toBe("#f85149");
  });

  it("respects custom color", () => {
    const { container } = render(<Sparkline data={[100, 110]} color="#ffffff" />);
    const polyline = container.querySelector("polyline");
    expect(polyline?.getAttribute("stroke")).toBe("#ffffff");
  });
});
