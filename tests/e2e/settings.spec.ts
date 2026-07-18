import { expect, test } from "@playwright/test";

import { MOCK, resetMocks } from "./helpers";

test.beforeEach(async ({ request }) => {
  await resetMocks(request);
});

test("settings round-trip: test connections, save, secrets come back masked", async ({
  page,
}) => {
  await page.goto("/settings/credentials");

  const catalyst = page.getByRole("region", { name: "Catalyst Center" });
  await catalyst.getByLabel("Base URL").fill(`${MOCK}/ccc`);
  await catalyst.getByLabel("Username").fill("admin");
  await catalyst.getByLabel("Password").fill("super-secret-pw");
  await catalyst.getByRole("button", { name: "Test connection" }).click();
  await expect(catalyst.getByRole("status")).toHaveText(
    /Connected\. \d+ sites visible\./,
  );

  const netbox = page.getByRole("region", { name: "NetBox" });
  await netbox.getByLabel("Base URL").fill(`${MOCK}/netbox`);
  await netbox.getByLabel("API token").fill("nb-token-12345");
  await netbox.getByRole("button", { name: "Test connection" }).click();
  await expect(netbox.getByRole("status")).toHaveText(
    /Connected\. NetBox 4\.2-mock\./,
  );

  const webhook = page.getByRole("region", { name: "ISE webhook" });
  await webhook.getByLabel("Target URL").fill(`${MOCK}/ise/hook`);
  await webhook.getByLabel(/Shared secret/).fill("hmac-secret-9876");
  await webhook.getByLabel("Enabled").check();

  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByText("Settings saved.")).toBeVisible();

  // Reload: secrets are write-only and shown masked, never echoed back
  await page.reload();
  const password = page
    .getByRole("region", { name: "Catalyst Center" })
    .getByLabel("Password");
  await expect(password).toHaveValue("");
  await expect(password).toHaveAttribute("placeholder", "****t-pw");
  await expect(
    page.getByRole("region", { name: "NetBox" }).getByLabel("API token"),
  ).toHaveAttribute("placeholder", "****2345");
  await expect(
    page
      .getByRole("region", { name: "ISE webhook" })
      .getByLabel(/Shared secret/),
  ).toHaveAttribute("placeholder", "****9876");
});
