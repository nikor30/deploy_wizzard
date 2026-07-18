import type { APIRequestContext } from "@playwright/test";

export const APP = "http://127.0.0.1:8061";
export const MOCK = "http://127.0.0.1:9100";

export async function resetMocks(
  request: APIRequestContext,
  devices = 2,
): Promise<void> {
  await request.post(`${MOCK}/__mock__/reset`, { data: { devices } });
}

/** Point the app at the mock stack and map the mock site (via the REST API). */
export async function configureApp(request: APIRequestContext): Promise<void> {
  await request.put(`${APP}/api/settings/credentials`, {
    data: {
      catalyst: { base_url: `${MOCK}/ccc`, username: "admin", secret: "pw" },
      netbox: { base_url: `${MOCK}/netbox`, secret: "nb-token-12345" },
      webhook: {
        base_url: `${MOCK}/ise/hook`,
        secret: "hmac-secret",
        enabled: true,
      },
    },
  });
  await request.put(`${APP}/api/mappings/sites`, {
    data: {
      mappings: [
        {
          netbox_site_id: 10,
          netbox_site_name: "FFM-DC1",
          ccc_site_id: "uuid-ffm",
          ccc_site_name: "Global/Germany/Frankfurt/DC1",
        },
      ],
    },
  });
  await request.put(`${APP}/api/settings/dayn`, {
    data: {
      mappings: [
        {
          variable: "SNMP_LOCATION",
          source_path: "device.custom_fields.snmp_location",
        },
        {
          variable: "NTP_SERVER",
          source_path: "device.config_context.ntp_server",
        },
      ],
    },
  });
}

/** Remove all jobs so each test starts from an empty wizard. */
export async function deleteAllJobs(request: APIRequestContext): Promise<void> {
  const jobs = (await (await request.get(`${APP}/api/wizard/jobs`)).json()) as {
    id: number;
  }[];
  for (const job of jobs) {
    await request.delete(`${APP}/api/wizard/jobs/${job.id}`);
  }
}
