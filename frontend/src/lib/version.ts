import type { TFunction } from "i18next";
import type { Meta } from "../api/types";

/** What the update notice says: the newer release, or (on :edge, a local build) how many commits are newer. */
export function updateHow(t: TFunction, version: NonNullable<Meta["version"]>): string {
  return version.latest ? t("update.release", { version: version.latest }) : t("update.how", { count: version.behind ?? 0 });
}
