export type AtsProviderDefaults = {
  source_url: string;
  search_text: string;
  limit: number;
  skip_interest_filter: boolean;
  skip_language_filter: boolean;
};

export type AtsProviderCatalog = Record<string, AtsProviderDefaults>;
export type PortalForm = AtsProviderDefaults & { provider: string };

export function portalFormForProvider(catalog: AtsProviderCatalog, provider: string): PortalForm | null {
  const defaults = catalog[provider];
  if (!defaults) return null;
  return {
    provider,
    source_url: defaults.source_url,
    search_text: defaults.search_text,
    limit: defaults.limit,
    skip_interest_filter: defaults.skip_interest_filter,
    skip_language_filter: defaults.skip_language_filter
  };
}

export function portalRequestBody(form: PortalForm): PortalForm {
  return {
    provider: form.provider,
    source_url: form.source_url,
    search_text: form.search_text,
    limit: form.limit,
    skip_interest_filter: form.skip_interest_filter,
    skip_language_filter: form.skip_language_filter
  };
}
