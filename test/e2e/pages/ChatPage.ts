import { expect, type Locator, type Page } from "@playwright/test";

/** Page object for the docked AI chat panel. */
export class ChatPage {
  readonly input: Locator;
  readonly sendButton: Locator;
  readonly assistantMessages: Locator;
  readonly actions: Locator;
  readonly thinking: Locator;

  constructor(page: Page) {
    this.input = page.getByLabel("Chat message");
    this.sendButton = page.getByRole("button", { name: "Send", exact: true });
    this.assistantMessages = page.locator('[data-testid="chat-message"][data-role="assistant"]');
    // Inline confirmations for trades / watchlist changes the AI made.
    this.actions = page.getByTestId("chat-action");
    this.thinking = page.getByRole("status", { name: "AI is thinking" });
  }

  /** Send a message and wait for the assistant's reply. Returns the reply text. */
  async send(text: string): Promise<string> {
    const before = await this.assistantMessages.count();
    await this.input.fill(text);
    await this.sendButton.click();
    await expect(this.assistantMessages).toHaveCount(before + 1);
    await expect(this.thinking).toBeHidden();
    return this.assistantMessages.last().innerText();
  }
}
