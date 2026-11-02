import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Nav } from "./Nav";
import { navigation, stubApi } from "@/test/fixtures";

vi.mock("next/navigation", async () => (await import("@/test/fixtures")).navigationModule);

async function renderAt(path: string) {
  navigation.reset(path);
  render(<Nav />);
  // The API status check settles after the first render.
  await screen.findByText("API online");
  const main = within(screen.getByRole("navigation", { name: "Main" }));
  return {
    documents: main.getByRole("link", { name: "Documents" }),
    search: main.getByRole("link", { name: "Search" }),
  };
}

beforeEach(() => {
  stubApi({ "GET /health": { status: "ok" } });
});

describe("Nav", () => {
  it("links to the documents and search pages", async () => {
    await renderAt("/");

    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Search" })).toHaveAttribute("href", "/search");
  });

  it.each([
    ["/", "documents"],
    ["/documents/abc", "documents"],
    ["/search", "search"],
    ["/search?q=cloud", "search"],
  ] as const)("marks the right link as current on %s", async (path, active) => {
    const links = await renderAt(path);
    const other = active === "documents" ? "search" : "documents";

    expect(links[active]).toHaveAttribute("aria-current", "page");
    expect(links[other]).not.toHaveAttribute("aria-current");
  });

  it("marks nothing as current on an unknown page", async () => {
    const links = await renderAt("/settings");

    expect(links.documents).not.toHaveAttribute("aria-current");
    expect(links.search).not.toHaveAttribute("aria-current");
  });

  it("shows the API status", async () => {
    await renderAt("/");

    expect(screen.getByRole("status")).toHaveTextContent("API online");
  });
});
