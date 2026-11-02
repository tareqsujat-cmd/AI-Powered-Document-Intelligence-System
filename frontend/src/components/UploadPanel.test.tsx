import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, uploadFile, type UploadOutcome } from "@/lib/api";
import { UploadPanel } from "./UploadPanel";
import { advance, flush, makeFile, makeOutcome, makeSummary } from "@/test/fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  uploadFile: vi.fn(),
}));

const uploadMock = vi.mocked(uploadFile);

interface PendingUpload {
  file: File;
  onProgress: (percent: number) => void;
  resolve: (outcome: UploadOutcome) => void;
  reject: (reason: unknown) => void;
}

/** Every call to uploadFile waits for the test to answer it. */
function pendingUploads(): PendingUpload[] {
  const pending: PendingUpload[] = [];
  uploadMock.mockImplementation((file, onProgress) => {
    const { promise, resolve, reject } = Promise.withResolvers<UploadOutcome>();
    pending.push({ file, onProgress, resolve, reject });
    return promise;
  });
  return pending;
}

async function chooseFiles(...files: File[]) {
  fireEvent.change(screen.getByLabelText("Choose files to upload"), { target: { files } });
  await flush();
}

async function settle(action: () => void) {
  await act(async () => action());
}

function rowOf(name: string) {
  const row = screen.getByText(name).closest("li");
  if (!row) throw new Error(`No row for ${name}`);
  return within(row);
}

const accepted = (id = "doc-9") => makeOutcome({ document: makeSummary({ id }) });
const tooMany = (seconds: number | null) => new ApiError("Too many requests.", 429, seconds);

beforeEach(() => {
  vi.useFakeTimers();
  uploadMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("UploadPanel: choosing files", () => {
  it("shows nothing but the prompt before any file is chosen", () => {
    render(<UploadPanel />);

    expect(screen.getByText(/Drag invoices here/)).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });

  it("offers the supported file types, several at a time", () => {
    render(<UploadPanel />);
    const input = screen.getByLabelText("Choose files to upload");

    expect(input).toHaveAttribute("multiple");
    expect(input).toHaveAttribute("accept", ".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv");
  });

  it("opens the file dialog from the choose files button", () => {
    render(<UploadPanel />);
    const input = screen.getByLabelText<HTMLInputElement>("Choose files to upload");
    const click = vi.spyOn(input, "click").mockImplementation(() => undefined);

    fireEvent.click(screen.getByRole("button", { name: "choose files" }));

    expect(click).toHaveBeenCalledTimes(1);
  });

  it("gives each file a row with its name and size", async () => {
    pendingUploads();
    render(<UploadPanel />);

    await chooseFiles(makeFile("a.pdf", 500), makeFile("b.csv", 2048));

    expect(rowOf("a.pdf").getByText("500 B")).toBeInTheDocument();
    expect(rowOf("b.csv").getByText("2 KB")).toBeInTheDocument();
  });

  it("takes dropped files", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    const zone = screen.getByText(/Drag invoices here/);
    const file = makeFile("dropped.png");

    fireEvent.dragOver(zone);
    fireEvent.drop(zone, { dataTransfer: { files: [file] } });
    await flush();

    expect(screen.getByText("dropped.png")).toBeInTheDocument();
    expect(pending[0].file).toBe(file);
  });

  it("does nothing when a drop has no files", async () => {
    render(<UploadPanel />);
    const zone = screen.getByText(/Drag invoices here/);

    fireEvent.drop(zone, { dataTransfer: { files: [] } });
    await flush();

    expect(uploadMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });
});

describe("UploadPanel: concurrency", () => {
  it("uploads at most 3 at once, and starts the next as each one ends", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    const names = ["1.pdf", "2.pdf", "3.pdf", "4.pdf", "5.pdf"];

    await chooseFiles(...names.map((name) => makeFile(name)));

    expect(uploadMock).toHaveBeenCalledTimes(3);
    expect(pending.map((upload) => upload.file.name)).toEqual(["1.pdf", "2.pdf", "3.pdf"]);
    expect(rowOf("4.pdf").getByText("Waiting…")).toBeInTheDocument();
    expect(rowOf("5.pdf").getByText("Waiting…")).toBeInTheDocument();

    await settle(() => pending[1].resolve(accepted()));

    expect(uploadMock).toHaveBeenCalledTimes(4);
    expect(pending[3].file.name).toBe("4.pdf");
    expect(rowOf("5.pdf").getByText("Waiting…")).toBeInTheDocument();
  });

  it("uploads every file in the end", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);

    await chooseFiles(...["1.pdf", "2.pdf", "3.pdf", "4.pdf"].map((name) => makeFile(name)));
    await settle(() => pending[0].reject(new Error("first failed")));
    await settle(() => {
      pending[1].resolve(accepted());
      pending[2].resolve(accepted());
    });
    await settle(() => pending[3].resolve(accepted()));

    expect(uploadMock).toHaveBeenCalledTimes(4);
    expect(screen.getAllByRole("link", { name: "View" })).toHaveLength(3);
    expect(rowOf("1.pdf").getByText("first failed")).toBeInTheDocument();
  });

  // BUG, components/UploadPanel.tsx:99-107: each addFiles call has its own queue and its own 3
  // workers, so a second batch added during the first goes over the limit.
  it("keeps to 3 at once when a second batch is added during the first", async () => {
    pendingUploads();
    render(<UploadPanel />);

    await chooseFiles(makeFile("a1.pdf"), makeFile("a2.pdf"), makeFile("a3.pdf"));
    await chooseFiles(makeFile("b1.pdf"), makeFile("b2.pdf"), makeFile("b3.pdf"));

    expect(uploadMock).toHaveBeenCalledTimes(3);
  });
});

describe("UploadPanel: outcomes", () => {
  it("shows the progress of an upload", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));
    const bar = () => screen.getByRole("progressbar", { name: "Uploading a.pdf" });

    expect(bar()).toHaveAttribute("aria-valuenow", "0");

    act(() => pending[0].onProgress(42));
    expect(bar()).toHaveAttribute("aria-valuenow", "42");

    act(() => pending[0].onProgress(100));
    expect(bar()).toHaveAttribute("aria-valuenow", "100");
  });

  it("links an accepted file to its document", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));

    await settle(() => pending[0].resolve(accepted("doc-42")));

    const row = rowOf("a.pdf");
    expect(row.getByText(/Uploaded and queued/)).toBeInTheDocument();
    expect(row.getByRole("link", { name: "View" })).toHaveAttribute("href", "/documents/doc-42");
    expect(row.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("shows the reason for a rejected file, with no link", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.exe"));

    await settle(() =>
      pending[0].resolve(
        makeOutcome({
          accepted: false,
          document: null,
          error: { code: "unsupported", reason: "This file type is not supported." },
        }),
      ),
    );

    const row = rowOf("a.exe");
    expect(row.getByText("This file type is not supported.")).toBeInTheDocument();
    expect(row.queryByRole("link")).not.toBeInTheDocument();
  });

  it("has a fallback when a rejection gives no reason", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));

    await settle(() =>
      pending[0].resolve(makeOutcome({ accepted: false, document: null, error: null })),
    );

    expect(rowOf("a.pdf").getByText("The file was not accepted.")).toBeInTheDocument();
  });

  it("shows the reason and a link for a file that failed on record", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));

    await settle(() =>
      pending[0].resolve(
        makeOutcome({
          document: makeSummary({ id: "doc-7" }),
          error: { code: "encrypted", reason: "The PDF is password protected." },
        }),
      ),
    );

    const row = rowOf("a.pdf");
    expect(row.getByText(/The PDF is password protected\./)).toBeInTheDocument();
    expect(row.getByRole("link", { name: "View" })).toHaveAttribute("href", "/documents/doc-7");
    expect(row.queryByText(/Uploaded and queued/)).not.toBeInTheDocument();
  });

  it("shows the message of a thrown error", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));

    await settle(() => pending[0].reject(new ApiError("The API could not be reached.", 0)));

    expect(rowOf("a.pdf").getByText("The API could not be reached.")).toBeInTheDocument();
    expect(uploadMock).toHaveBeenCalledTimes(1);
  });

  it("has a fallback for a thrown value that is not an Error", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));

    await settle(() => pending[0].reject("nope"));

    expect(rowOf("a.pdf").getByText("The upload failed.")).toBeInTheDocument();
  });

  it("calls onUploaded when the API has answered, whatever the answer", async () => {
    const pending = pendingUploads();
    const onUploaded = vi.fn();
    render(<UploadPanel onUploaded={onUploaded} />);
    await chooseFiles(makeFile("a.pdf"), makeFile("b.pdf"));
    expect(onUploaded).not.toHaveBeenCalled();

    await settle(() => pending[0].resolve(accepted()));
    expect(onUploaded).toHaveBeenCalledTimes(1);

    await settle(() =>
      pending[1].resolve(
        makeOutcome({
          accepted: false,
          document: null,
          error: { code: "too_large", reason: "Too large." },
        }),
      ),
    );
    expect(onUploaded).toHaveBeenCalledTimes(2);
  });

  it("does not call onUploaded when the upload never reached the API", async () => {
    const pending = pendingUploads();
    const onUploaded = vi.fn();
    render(<UploadPanel onUploaded={onUploaded} />);
    await chooseFiles(makeFile("a.pdf"));

    await settle(() => pending[0].reject(new Error("Network down")));

    expect(onUploaded).not.toHaveBeenCalled();
  });
});

describe("UploadPanel: the rate limit", () => {
  it("waits for retryAfterSeconds, then retries the same file", async () => {
    const pending = pendingUploads();
    const file = makeFile("a.pdf");
    render(<UploadPanel />);
    await chooseFiles(file);

    await settle(() => pending[0].reject(tooMany(7)));

    const row = rowOf("a.pdf");
    expect(row.getByText(/Trying again in 7 s/)).toBeInTheDocument();
    expect(row.queryByRole("progressbar")).not.toBeInTheDocument();

    await advance(6999);
    expect(uploadMock).toHaveBeenCalledTimes(1);

    await advance(1);
    expect(uploadMock).toHaveBeenCalledTimes(2);
    expect(pending[1].file).toBe(file);
    expect(rowOf("a.pdf").getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");

    await settle(() => pending[1].resolve(accepted("doc-5")));
    expect(rowOf("a.pdf").getByRole("link", { name: "View" })).toHaveAttribute(
      "href",
      "/documents/doc-5",
    );
  });

  it("waits 5 seconds when the API gives no retry time", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));

    await settle(() => pending[0].reject(tooMany(null)));

    expect(screen.getByText(/Trying again in 5 s/)).toBeInTheDocument();
    await advance(4999);
    expect(uploadMock).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(uploadMock).toHaveBeenCalledTimes(2);
  });

  it("gives up after the retry limit and shows the message", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));

    // One first attempt and five retries.
    for (let attempt = 0; attempt < 5; attempt += 1) {
      await settle(() => pending[attempt].reject(tooMany(1)));
      expect(screen.getByText(/Trying again in 1 s/)).toBeInTheDocument();
      await advance(1000);
      expect(uploadMock).toHaveBeenCalledTimes(attempt + 2);
    }
    await settle(() => pending[5].reject(tooMany(1)));

    expect(rowOf("a.pdf").getByText("Too many requests.")).toBeInTheDocument();
    await advance(60000);
    expect(uploadMock).toHaveBeenCalledTimes(6);
  });

  it("keeps other uploads going while one waits", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"), makeFile("b.pdf"));

    await settle(() => pending[0].reject(tooMany(3)));

    expect(rowOf("b.pdf").getByRole("progressbar")).toBeInTheDocument();
    expect(rowOf("a.pdf").getByText(/Trying again in 3 s/)).toBeInTheDocument();
  });
});

describe("UploadPanel: Clear", () => {
  it("only appears when nothing is uploading or waiting, and removes the rows", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"), makeFile("b.pdf"));
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
    expect(screen.getByText("Uploading…")).toBeInTheDocument();

    await settle(() => pending[0].resolve(accepted()));
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();

    await settle(() => pending[1].reject(new Error("Failed")));
    expect(screen.queryByText("Uploading…")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });

  it("stays hidden while files are queued behind a waiting upload", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(...["1.pdf", "2.pdf", "3.pdf", "4.pdf"].map((name) => makeFile(name)));

    await settle(() => pending[0].reject(tooMany(5)));

    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });

  // BUG, components/UploadPanel.tsx:112-114: `inProgress` leaves out the throttled phase, so with
  // only a throttled row Clear appears. Pressing it hides the row while the upload still retries.
  it("stays hidden while the only remaining upload waits on the rate limit", async () => {
    const pending = pendingUploads();
    render(<UploadPanel />);
    await chooseFiles(makeFile("a.pdf"));

    await settle(() => pending[0].reject(tooMany(5)));

    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });
});
