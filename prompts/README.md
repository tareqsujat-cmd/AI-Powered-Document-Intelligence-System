# Prompts

Every LLM prompt the application uses at runtime lives here as a Markdown file, so prompts are reviewed and versioned like code. Extraction results record the prompt version that produced them, so raise `version` in a prompt's header whenever its meaning changes.

| Prompt | Used for |
|---|---|
| [invoice_extraction.md](invoice_extraction.md) | Extracting invoice fields and line items from document text |
