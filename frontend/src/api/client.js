const getJson = async (path, options = {}) => {
  const response = await fetch(path, { cache: "no-store", signal: options.signal });
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
};

const getJsonWithHeader = async (path, headerName) => {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  const data = await response.json();
  return { data, headerValue: response.headers.get(headerName) };
};

export const buildPath = (path, filters = {}, extra = {}) => {
  const params = new URLSearchParams();
  if (filters.faculty && filters.faculty !== "All faculties") {
    params.set("faculty", filters.faculty);
  }
  if (filters.department && filters.department !== "All departments") {
    params.set("department", filters.department);
  }
  if (Array.isArray(filters.publicationYears)) {
    if (filters.publicationYears.length > 0) {
      params.set("publication_years", filters.publicationYears.join(","));
      filters.publicationYears.forEach((year) => {
        params.append("publication_year", String(year));
      });
      params.set("_filter_years", filters.publicationYears.join("-"));
    }
  } else if (filters.publicationYear && filters.publicationYear !== "All years") {
    params.set("publication_year", filters.publicationYear);
  }
  if (filters.articleOnly) {
    params.set("article_only", "true");
  }
  if (filters.researcherQuery && filters.researcherQuery.trim()) {
    params.set("researcher", filters.researcherQuery.trim());
  }
  if (filters.publicationTitleQuery && filters.publicationTitleQuery.trim()) {
    params.set("publication_title", filters.publicationTitleQuery.trim());
  }
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== null) {
      params.set(key, String(value));
    }
  });
  const query = params.toString();
  return query ? `${path}?${query}` : path;
};

export const fetchDashboard = async (filters = {}) =>
  getJson(buildPath("/api/dashboard", filters));

// Returns both the rows and the backend's X-Researchers-Cache status ("HIT" | "SNAPSHOT" |
// "FAST" | "REBUILDING" | "REBUILT" | "BYPASS"). When the backend is still building its researcher
// index for the first time it responds with an empty list and status "REBUILDING" rather than
// an error, so callers can tell "no data yet" apart from "genuinely no data" and retry.
export const fetchResearchers = async (filters = {}) => {
  const { data, headerValue } = await getJsonWithHeader(
    buildPath("/api/researchers", filters, {
      limit: filters.researcherLimit ?? 20,
      page: filters.researcherPage ?? undefined,
      page_size: filters.researcherPageSize ?? filters.researcherLimit ?? 20,
      alphabet: filters.researcherAlphabet || undefined,
    }),
    "X-Researchers-Cache",
  );
  if (Array.isArray(data)) {
    return {
      rows: data,
      meta: {
        total: data.length,
        page: 1,
        page_size: data.length,
        total_pages: 1,
      },
      cacheStatus: headerValue,
    };
  }
  return { rows: data.rows ?? [], meta: data, cacheStatus: headerValue };
};

export const fetchPublications = async (filters = {}) =>
  getJson(buildPath("/api/publications", filters, { limit: 50 }));

export const fetchPublicationsBrowse = async (filters = {}) =>
  getJson(
    buildPath("/api/publications/browse", filters, {
      page: filters.page ?? 1,
      page_size: filters.pageSize ?? 20,
      alphabet: filters.alphabet || undefined,
      search: filters.publicationSearch || undefined,
    }),
  );

export const fetchResearchFields = async (filters = {}) =>
  getJson(buildPath("/api/research-fields", filters, { limit: 50 }));

export const fetchResearcherProfile = async (researcher, filters = {}, options = {}) =>
  getJson(buildPath("/api/researchers/profile", filters, { researcher }), options);

export const articleMetadataDownloadPath = (filters = {}) =>
  buildPath("/api/publications/article-metadata/download", filters);
