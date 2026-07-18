import { expect, test } from "@playwright/test";

import { configureApp, deleteAllJobs, resetMocks } from "./helpers";

// Runs in the 'mobile' project (Pixel 5 viewport, chromium).
test.beforeEach(async ({ request }) => {
  await resetMocks(request);
  await configureApp(request);
  await deleteAllJobs(request);
});

test("wizard and navigation are usable on a phone viewport", async ({
  page,
}) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Onboarding Wizard" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start new onboarding job" }),
  ).toBeVisible();

  // no horizontal page scroll on mobile
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);

  // device table stays reachable and scrolls inside its own container
  await page.getByRole("button", { name: "Start new onboarding job" }).click();
  await expect(page.getByLabel("Select SN000001")).toBeVisible();

  // main navigation reaches settings + stats + logs
  for (const target of ["Statistics", "Logs", "Credentials", "Site Mapping"]) {
    await expect(page.getByRole("link", { name: target })).toBeVisible();
  }
});
