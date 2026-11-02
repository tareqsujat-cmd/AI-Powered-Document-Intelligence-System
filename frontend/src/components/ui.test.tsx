import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  Button,
  ConfidenceMeter,
  ErrorNote,
  FormLabel,
  ProgressBar,
  ReviewBadge,
  SeverityBadge,
  StatusBadge,
} from "./ui";

describe("Button", () => {
  it("does not submit a form unless asked to", () => {
    const onSubmit = vi.fn((event: React.FormEvent) => event.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <Button>Plain</Button>
        <Button type="submit">Send</Button>
      </form>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Plain" }));
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("does not fire when disabled", () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Off
      </Button>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Off" }));

    expect(onClick).not.toHaveBeenCalled();
  });
});

describe("badges and notes", () => {
  it.each([
    ["queued", "Queued"],
    ["processing", "Processing"],
    ["completed", "Completed"],
    ["failed", "Failed"],
  ] as const)("shows the status %s as %s", (status, label) => {
    render(<StatusBadge status={status} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("shows the review badge", () => {
    render(<ReviewBadge />);
    expect(screen.getByText("Needs review")).toBeInTheDocument();
  });

  it.each(["error", "warning", "info"] as const)("shows the severity %s", (severity) => {
    render(<SeverityBadge severity={severity} />);
    expect(screen.getByText(severity)).toBeInTheDocument();
  });

  it("announces an error note", () => {
    render(<ErrorNote>It broke</ErrorNote>);
    expect(screen.getByRole("alert")).toHaveTextContent("It broke");
  });
});

describe("ProgressBar", () => {
  it.each([
    [42, 42],
    [-5, 0],
    [130, 100],
  ])("reports %i percent as %i", (percent, shown) => {
    render(<ProgressBar percent={percent} label="Upload" />);
    expect(screen.getByRole("progressbar", { name: "Upload" })).toHaveAttribute(
      "aria-valuenow",
      String(shown),
    );
  });
});

describe("ConfidenceMeter", () => {
  it("shows the confidence as a percentage, with a title", () => {
    render(<ConfidenceMeter confidence={0.87} />);
    expect(screen.getByText("87%")).toBeInTheDocument();
    expect(screen.getByTitle("Confidence 87%")).toBeInTheDocument();
  });
});

describe("FormLabel", () => {
  it("names the control inside it", () => {
    render(
      <FormLabel label="Supplier">
        <input />
      </FormLabel>,
    );
    expect(screen.getByRole("textbox", { name: "Supplier" })).toBeInTheDocument();
  });
});
