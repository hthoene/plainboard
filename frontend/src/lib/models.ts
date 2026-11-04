import type { ProviderInfo } from "../api/types";

/** The provider's smallest model, e.g. for summaries and connection checks. */
export function smallModel(provider?: ProviderInfo): string | undefined {
  return provider?.models.find((m) => m.tier === "small")?.id ?? provider?.models[0]?.id;
}

/** The target provider's model of the same size (tier) as `model`, which may belong to any provider. */
export function equivalentModel(providers: ProviderInfo[], model: string, target: string): string {
  const models = providers.find((p) => p.name === target)?.models ?? [];
  if (!models.length || models.some((m) => m.id === model)) return model;
  const tier = providers.flatMap((p) => p.models).find((m) => m.id === model)?.tier;
  return (models.find((m) => m.tier === tier) ?? models[0]).id;
}
