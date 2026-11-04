import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import type { ProviderAccount } from "../../api/types";
import { Button } from "../../components/ui";

const KEY = ["providerAccount", "codex"];

/** The ChatGPT subscription signed in on the runner, with a device-code sign-in. */
export function CodexSignIn() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const account = useQuery({
    queryKey: KEY,
    queryFn: () => api.get<ProviderAccount>("/providers/codex/account"),
    refetchInterval: (query) => (query.state.data?.login_code ? 3000 : false),
  });

  const act = async (action: "login" | "logout") => {
    setBusy(true);
    setError(null);
    try {
      qc.setQueryData(KEY, await api.post<ProviderAccount>(`/providers/codex/${action}`));
      if (action === "logout") await qc.invalidateQueries({ queryKey: KEY });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const data = account.data;
  const problem = error ?? data?.login_error ?? (account.error ? errorMessage(account.error) : null);
  return (
    <div className="space-y-2 rounded-md bg-zinc-50 p-3 text-xs text-zinc-600 dark:bg-zinc-800/50 dark:text-zinc-400">
      {data?.login_code ? (
        <>
          <p>{t("settings.codexDeviceCode")}</p>
          <a href={data.login_url ?? "#"} target="_blank" rel="noreferrer" className="block font-medium break-all text-indigo-600 underline dark:text-indigo-400">
            {data.login_url}
          </a>
          <p className="font-mono text-lg font-semibold tracking-widest text-zinc-900 select-all dark:text-zinc-100">{data.login_code}</p>
          <div className="flex items-center gap-3">
            <span className="animate-pulse">{t("settings.codexWaiting")}</span>
            <Button size="sm" variant="ghost" onClick={() => act("logout")} loading={busy}>
              {t("settings.codexCancel")}
            </Button>
          </div>
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <span className="min-w-0 flex-1">{data?.signed_in ? t("settings.codexSignedIn", { detail: data.detail }) : account.isLoading ? t("settings.testing") : t("settings.codexSignedOut")}</span>
          <Button size="sm" onClick={() => act(data?.signed_in ? "logout" : "login")} loading={busy}>
            {data?.signed_in ? t("settings.codexSignOut") : t("settings.codexSignIn")}
          </Button>
        </div>
      )}
      {problem && <p className="text-red-600">{problem}</p>}
      {!data?.signed_in && !data?.login_code && (
        <p>
          {t("settings.codexShellHint")} <code className="font-mono">docker compose exec -it runner codex login --device-auth</code>
        </p>
      )}
    </div>
  );
}
