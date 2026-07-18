import { expect, test } from "@playwright/test";

import { MOCK, configureApp, deleteAllJobs, resetMocks } from "./helpers";

test.beforeEach(async ({ request }) => {
  await resetMocks(request);
  await configureApp(request);
  await deleteAllJobs(request);
});

test("complete wizard run: claim, resume after reload, Day-N manual entry, finalize", async ({
  page,
  request,
}) => {
  await page.goto("/");

  // Step 1 — select unclaimed devices
  await page.getByRole("button", { name: "Start new onboarding job" }).click();
  await page.getByLabel("Select SN000001").check();
  await page.getByLabel("Select SN000002").check();
  await page.getByRole("button", { name: "Continue with 2 device(s)" }).click();

  // Step 2 — match: requirements hint + both devices matched
  await expect(page.getByText("same serial number")).toBeVisible();
  await expect(page.getByText("matched", { exact: true })).toHaveCount(2);
  await page
    .getByRole("region", { name: "Match SN000001" })
    .getByLabel(/Mgmt VLAN/i)
    .selectOption("110");

  // Resume after reload: the server-side job survives the browser
  await page.reload();
  await page.getByRole("button", { name: "Resume" }).click();
  await expect(page.getByText("same serial number")).toBeVisible();
  await expect(page.getByText("matched", { exact: true })).toHaveCount(2);
  await page
    .getByRole("button", { name: "Continue to Day-0 claim (2 device(s))" })
    .click();

  // Step 3 — Day-0 claim with live progress
  await page
    .getByLabel(/Onboarding template/i)
    .selectOption({ label: "PnP / Day0 Onboarding" });
  await page
    .getByRole("button", { name: "Start Day-0 claim (2 device(s))" })
    .click();
  await expect(page.getByRole("status")).toHaveText(
    /Day-0 finished: 2 succeeded, 0 failed/,
    {
      timeout: 60_000,
    },
  );

  // Webhook fired per device, HMAC-signed
  const mockState = await (await request.get(`${MOCK}/__mock__/state`)).json();
  expect(mockState.deliveries).toHaveLength(2);
  expect(mockState.deliveries[0].signature).toBeTruthy();

  // Step 4 — Day-N: mapped variables read-only, CONTACT is manual entry
  await page
    .getByRole("button", { name: "Continue to Day-N (2 device(s))" })
    .click();
  await page
    .getByRole("combobox", { name: "Template", exact: true })
    .selectOption({ label: "Baseline / DayN Baseline" });
  await page.getByRole("button", { name: "Resolve variables" }).click();
  await expect(page.getByText("FFM DC1 / Rack 4").first()).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Deploy Day-N/ }),
  ).toBeDisabled();
  await expect(
    page.getByText("Fill in all manual variables first."),
  ).toBeVisible();
  for (const input of await page.getByLabel(/CONTACT \(manual\)/i).all()) {
    await input.fill("noc@example.com");
  }
  await page
    .getByRole("button", { name: "Deploy Day-N (2 device(s))" })
    .click();

  // Step 5 — finalize: job completed, NetBox devices set active
  await expect(page.getByRole("status")).toHaveText(
    /completed: 2 device\(s\) active in\s+NetBox/,
    {
      timeout: 60_000,
    },
  );
  const finalState = await (await request.get(`${MOCK}/__mock__/state`)).json();
  expect(Object.values(finalState.netbox_statuses)).toEqual([
    "active",
    "active",
  ]);
});

test("half-failed batch: sibling succeeds, job resumable from the list", async ({
  page,
  request,
}) => {
  await request.post(`${MOCK}/__mock__/config`, {
    data: { fail_onboarding_serials: ["SN000002"] },
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Start new onboarding job" }).click();
  await page.getByLabel("Select SN000001").check();
  await page.getByLabel("Select SN000002").check();
  await page.getByRole("button", { name: "Continue with 2 device(s)" }).click();
  await expect(page.getByText("matched", { exact: true })).toHaveCount(2);
  await page
    .getByRole("button", { name: "Continue to Day-0 claim (2 device(s))" })
    .click();
  await page
    .getByLabel(/Onboarding template/i)
    .selectOption({ label: "PnP / Day0 Onboarding" });
  await page
    .getByRole("button", { name: "Start Day-0 claim (2 device(s))" })
    .click();

  await expect(page.getByRole("status")).toHaveText(
    /Day-0 finished: 1 succeeded, 1 failed/,
    {
      timeout: 60_000,
    },
  );
  await expect(page.getByText("onboarding failed on device")).toBeVisible();
  // the successful sibling can continue to Day-N
  await expect(
    page.getByRole("button", { name: "Continue to Day-N (1 device(s))" }),
  ).toBeEnabled();
});
