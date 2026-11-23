import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { qk } from "../../api/queries";
import type { ProviderAccount } from "../../api/types";
import { Button, Input } from "../../components/ui";

/** The Google account of an Antigravity subscription, signed in on the runner via OAuth link + pasted code. */
export function AntigravitySignIn({ accountId }: { accountId: number }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const KEY = ["accountSignIn", accountId];
  const url = `/accounts/${accountId}/signin`;
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const account = useQuery({
    queryKey: KEY,
    queryFn: () => api.get<ProviderAccount>(url),
  });

  const act = async (action: "login" | "submit" | "logout") => {
    setBusy(true);
    setError(null);
    try {
      const next =
        action === "login"
          ? await api.post<ProviderAccount>(url)
          : action === "submit"
            ? await api.post<ProviderAccount>(url, { code })
            : await api.del<ProviderAccount>(url);
      qc.setQueryData(KEY, next);
      if (action === "submit") {
        setCode("");
        await Promise.all([
          qc.invalidateQueries({ queryKey: qk.meta }),
          qc.invalidateQueries({ queryKey: qk.limits }),
        ]);
      } else if (action === "logout") {
        setCode("");
        await Promise.all([
          qc.invalidateQueries({ queryKey: KEY }),
          qc.invalidateQueries({ queryKey: qk.limits }),
        ]);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const data = account.data;
  const problem = error ?? data?.login_error ?? (account.error ? errorMessage(account.error) : null);
  const waiting = Boolean(data?.login_url && data?.login_input);
  return (
    <div className="space-y-2 rounded-sm bg-sunk p-3 text-fg-2">
      {waiting ? (
        <>
          <p>{t("settings.antigravityPasteCode")}</p>
          <a href={data?.login_url ?? "#"} target="_blank" rel="noreferrer" className="block font-medium break-all text-fg underline">
            {data?.login_url}
          </a>
          <div className="flex flex-wrap gap-2">
            <Input
              className="min-w-0 flex-1 font-mono"
              autoComplete="off"
              placeholder={t("settings.antigravityCodePlaceholder")}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && code.trim() && !busy && act("submit")}
            />
            <Button size="sm" variant="primary" onClick={() => act("submit")} disabled={!code.trim()} loading={busy}>
              {t("settings.antigravitySubmitCode")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => act("logout")} loading={busy}>
              {t("settings.antigravityCancel")}
            </Button>
          </div>
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <span className="min-w-0 flex-1">
            {data?.signed_in
              ? t("settings.antigravitySignedIn", { detail: data.detail })
              : account.isLoading
                ? t("settings.testing")
                : t("settings.antigravitySignedOut")}
          </span>
          <Button size="sm" onClick={() => act(data?.signed_in ? "logout" : "login")} loading={busy}>
            {data?.signed_in ? t("settings.antigravitySignOut") : t("settings.antigravitySignIn")}
          </Button>
        </div>
      )}
      {problem && <p className="text-you-fg">{problem}</p>}
    </div>
  );
}
