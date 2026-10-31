import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import Home from "./page";

it("renders the title and the API status indicator", () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

  render(<Home />);

  expect(
    screen.getByRole("heading", { level: 1, name: "Document Intelligence" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Checking API");
});
