import { expect, type Page, test } from "@playwright/test";

const run = String(Date.now()).slice(-7);
const SHOTS = "e2e-artifacts/screenshots";

function pdfWith(lines: string[]): Buffer {
  const text = lines
    .map((line) => `(${line.replace(/([()\\])/g, "\\$1")}) Tj T*`)
    .join("\n");
  const stream = `BT /F1 11 Tf 16 TL 50 780 Td\n${text}\nET`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((object, index) => {
    offsets.push(body.length);
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const start = body.length;
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  body += offsets.map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("");
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF\n`;
  return Buffer.from(body, "latin1");
}

function invoicePdf(number: string, total = "1,100.00"): Buffer {
  return pdfWith([
    "INVOICE",
    `Invoice Number: ${number}`,
    "Invoice Date: 2026-05-01",
    "Supplier: Northwind Traders",
    "Bill To: Contoso Retail",
    "Consulting services 2 500.00 1,000.00",
    "Subtotal 1,000.00",
    "Tax 100.00",
    `Total GBP ${total}`,
  ]);
}

function invoiceCsv(number: string, total: string): Buffer {
  return Buffer.from(
    "Invoice No,Invoice Date,Supplier,Customer,Subtotal,Tax,Total,Currency\n" +
      `${number},2026-05-01,Northwind Traders,Contoso Retail,1000.00,100.00,${total},GBP\n`,
  );
}

function rowFor(page: Page, name: string) {
  return page.getByRole("row").filter({ has: page.getByRole("link", { name }) });
}

async function waitForFinished(page: Page, name: string) {
  await expect(rowFor(page, name).getByText(/^(Completed|Failed)$/)).toBeVisible({
    timeout: 120_000,
  });
}

test("a person can upload, follow, review, correct, and search invoices", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await test.step("upload several files and watch them finish", async () => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible();
    await expect(page.getByText("API online")).toBeVisible();

    const files = [
      { name: `invoice-${run}.pdf`, mimeType: "application/pdf", buffer: invoicePdf(`E2E-${run}`) },
      { name: `sheet-${run}.csv`, mimeType: "text/csv", buffer: invoiceCsv(`CSV-${run}`, "1100.00") },
      { name: `damaged-${run}.pdf`, mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7\nnot a pdf") },
      { name: `virus-${run}.pdf`, mimeType: "application/pdf", buffer: Buffer.from([0x4d, 0x5a, 0x90, 0x00, ...new Array(64).fill(0)]) },
    ];
    await page.getByLabel("Choose files to upload").setInputFiles(files);

    // The disguised program is refused at upload, with a reason.
    await expect(page.getByText("not a supported document type")).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/1-uploading.png`, fullPage: true });

    await waitForFinished(page, `invoice-${run}.pdf`);
    await waitForFinished(page, `sheet-${run}.csv`);
    await waitForFinished(page, `damaged-${run}.pdf`);

    await expect(rowFor(page, `invoice-${run}.pdf`).getByText("Completed")).toBeVisible();
    await expect(rowFor(page, `sheet-${run}.csv`).getByText("Completed")).toBeVisible();
    await expect(rowFor(page, `damaged-${run}.pdf`).getByText("could not be opened")).toBeVisible();
    await expect(page.getByRole("link", { name: `virus-${run}.pdf` })).toHaveCount(0);
    await page.screenshot({ path: `${SHOTS}/2-dashboard.png`, fullPage: true });
  });

  await test.step("filters narrow the list and survive going back", async () => {
    await page.getByLabel("File name").fill(`sheet-${run}`);
    await expect(page.getByRole("link", { name: `invoice-${run}.pdf` })).toHaveCount(0);
    await expect(page.getByRole("link", { name: `sheet-${run}.csv` })).toBeVisible();
    await expect(page).toHaveURL(/filename=/);

    await page.getByRole("link", { name: `sheet-${run}.csv` }).click();
    await expect(page.getByRole("heading", { name: `sheet-${run}.csv` })).toBeVisible();
    // A spreadsheet preview is a table of its cells, not a picture.
    await expect(page.getByRole("cell", { name: `CSV-${run}` })).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/3-spreadsheet.png`, fullPage: true });

    await page.goBack();
    await expect(page.getByLabel("File name")).toHaveValue(`sheet-${run}`);
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(page.getByRole("link", { name: `invoice-${run}.pdf` })).toBeVisible();
  });

  await test.step("the detail page shows the page beside the extracted values", async () => {
    await page.getByRole("link", { name: `invoice-${run}.pdf` }).click();
    await expect(page.getByRole("heading", { name: `invoice-${run}.pdf` })).toBeVisible();

    const preview = page.getByAltText(`Page 1 of invoice-${run}.pdf`);
    await expect(preview).toBeVisible();
    await expect
      .poll(() => preview.evaluate((image: HTMLImageElement) => image.naturalWidth))
      .toBeGreaterThan(500);

    await expect(page.getByText(`E2E-${run}`, { exact: true })).toBeVisible();
    await expect(page.getByText("1100.00", { exact: true })).toBeVisible();
    await expect(page.getByText("Northwind Traders", { exact: true })).toBeVisible();
    await expect(page.getByRole("cell", { name: /Consulting services/ })).toBeVisible();
    await expect(page.getByText("All checks passed.")).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/4-detail.png`, fullPage: true });
  });

  await test.step("correcting a value re-validates, and a review clears the flag", async () => {
    await page.getByRole("button", { name: "Edit Total" }).click();
    const input = page.getByRole("textbox", { name: "Total" });
    await expect(input).toHaveValue("1100.00");
    await input.fill("1,500.00");
    await page.getByRole("button", { name: "Save", exact: true }).click();

    await expect(page.getByText("1500.00", { exact: true })).toBeVisible();
    await expect(page.getByText(/plus tax .* but the total is 1500.00/)).toBeVisible();
    await expect(page.getByText("Needs review")).toBeVisible();
    await expect(page.getByText("Corrected by reviewer.")).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/5-correction.png`, fullPage: true });

    await page.getByRole("button", { name: "Mark reviewed" }).first().click();
    await expect(page.getByText("Reviewed", { exact: true }).first()).toBeVisible();

    await page.getByRole("button", { name: "History" }).click();
    await expect(page.getByText(/"total":"1100.00".*"total":"1500.00"/)).toBeVisible();

    // Put it back, which removes the problem that was just reviewed.
    await page.getByRole("button", { name: "Edit Total" }).click();
    await page.getByRole("textbox", { name: "Total" }).fill("1100.00");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("All checks passed.")).toBeVisible();
  });

  await test.step("a bad value is refused with a reason, and nothing is lost", async () => {
    await page.getByRole("button", { name: "Edit Tax" }).click();
    await page.getByRole("textbox", { name: "Tax" }).fill("lots");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Enter an amount as a number")).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Tax" })).toHaveValue("lots");
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByText("100.00", { exact: true })).toBeVisible();
  });

  await test.step("a failed document explains itself and can be retried", async () => {
    await page.goto("/");
    await page.getByRole("link", { name: `damaged-${run}.pdf` }).click();
    await expect(page.getByText("The PDF is damaged and could not be opened.").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/6-failed.png`, fullPage: true });
  });

  await test.step("reprocessing warns first, and cancelling does nothing", async () => {
    await page.goto("/");
    await page.getByRole("link", { name: `invoice-${run}.pdf` }).click();
    await page.getByRole("button", { name: "Reprocess", exact: true }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog.getByText(/including any corrections/)).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByText("Completed", { exact: true })).toBeVisible();
  });

  await test.step("search finds it by number, by word, and by meaning", async () => {
    await page.getByRole("link", { name: "Search" }).click();
    await expect(page.getByRole("heading", { name: "Search" })).toBeVisible();

    await page.getByRole("searchbox").fill(`E2E-${run}`);
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByRole("link", { name: `invoice-${run}.pdf` })).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/7-search.png`, fullPage: true });

    await page.locator("label", { hasText: "Semantic" }).click();
    await page.getByRole("searchbox").fill("professional advice fees");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByText("semantic").first()).toBeVisible();

    await page.getByRole("searchbox").fill("zzzzqqqq xxyyzz");
    await page.locator("label", { hasText: "Keyword" }).click();
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByText(/Nothing matched/)).toBeVisible();

    await page.locator("label", { hasText: "Keyword" }).click();
    await page.getByRole("searchbox").fill(`E2E-${run}`);
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.getByRole("link", { name: `invoice-${run}.pdf` }).click();
    await expect(page).toHaveURL(/page=1/);
    await expect(page.getByAltText(`Page 1 of invoice-${run}.pdf`)).toBeVisible();
  });

  await test.step("the layout works on a phone", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    await page.screenshot({ path: `${SHOTS}/8-phone.png`, fullPage: true });

    await page.getByRole("link", { name: `invoice-${run}.pdf` }).click();
    await expect(page.getByRole("heading", { name: `invoice-${run}.pdf` })).toBeVisible();
    const detailOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(detailOverflow).toBeLessThanOrEqual(0);
    await page.screenshot({ path: `${SHOTS}/9-phone-detail.png`, fullPage: true });
  });

  // A failed preview is expected for a damaged file, and shows as a message on the page.
  const unexpected = consoleErrors.filter(
    (text) => !/404|422|Failed to load resource/.test(text),
  );
  expect(unexpected).toEqual([]);
});
