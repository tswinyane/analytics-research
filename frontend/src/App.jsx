import { useEffect, useRef, useState } from "react";
import {
  fetchDashboard,
  fetchPublicationsBrowse,
  fetchResearchFields,
  fetchResearcherProfile,
  fetchResearchers,
} from "./api/client";
import BarList from "./components/BarList";
import ColumnChart from "./components/ColumnChart";
import DataTable from "./components/DataTable";
import MetricCard from "./components/MetricCard";
import PieChart from "./components/PieChart";
import TopicTrendChart from "./components/TopicTrendChart";

const defaultFaculty = "All faculties";
const defaultDepartment = "All departments";
const defaultPublicationYear = "All years";
const articleOnlyPublicationFilters = { articleOnly: true };
const openAlexUnmappedLabel = "Unmapped";
const visibleMappingValue = (value) => {
  const text = String(value ?? "").trim();
  return text;
};
const visibleMappingValues = (values = []) => values.map(visibleMappingValue).filter(Boolean);
const uniqueFilterValues = (values) => Array.from(new Set(values.filter(Boolean)));
const hasVisibleFacultyAndDepartment = (row) =>
  Boolean(visibleMappingValue(row?.faculty)) && Boolean(visibleMappingValue(row?.department));
const publicationYearFilterStart = 1894;
const publicationYearFilterEnd = 2026;
const publicationYearPreviewStart = 2020;
const publicationYearPreviewEnd = 2026;
const researcherPageSize = 20;
const publicationPageSize = 20;
const alphabetFilters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");
const pages = [
  { id: "dashboard", label: "Dashboard" },
  { id: "researchers", label: "Researchers" },
  { id: "publications", label: "Publications" },
  { id: "fields", label: "Research fields" },
  { id: "sdgs", label: "SDG keywords" },
];
const pageIds = new Set(pages.map((page) => page.id));
const pageFromPath = (pathname = window.location.pathname) => {
  const candidate = pathname.replace(/^\/+|\/+$/g, "") || "dashboard";
  return pageIds.has(candidate) ? candidate : "dashboard";
};
const pathForPage = (pageId) => (pageId === "dashboard" ? "/" : `/${pageId}`);

export default function App() {
  const [dashboard, setDashboard] = useState(null);
  const [researchers, setResearchers] = useState([]);
  const [researchersMeta, setResearchersMeta] = useState({
    total: 0,
    page: 1,
    page_size: researcherPageSize,
    total_pages: 1,
  });
  const [publications, setPublications] = useState([]);
  const [publicationsMeta, setPublicationsMeta] = useState({
    total: 0,
    page: 1,
    page_size: publicationPageSize,
    total_pages: 1,
  });
  const [publicationPage, setPublicationPage] = useState(1);
  const [publicationAlphabet, setPublicationAlphabet] = useState("");
  const [publicationSearch, setPublicationSearch] = useState("");
  const [researchFields, setResearchFields] = useState([]);
  const [selectedPage, setSelectedPage] = useState(() => pageFromPath());
  const [selectedFaculty, setSelectedFaculty] = useState(defaultFaculty);
  const [selectedDepartment, setSelectedDepartment] = useState(defaultDepartment);
  const [selectedPublicationYears, setSelectedPublicationYears] = useState([]);
  const [researcherQuery, setResearcherQuery] = useState("");
  const [debouncedResearcherQuery, setDebouncedResearcherQuery] = useState("");
  const [researcherPage, setResearcherPage] = useState(1);
  const [researcherAlphabet, setResearcherAlphabet] = useState("");
  const [showPublicationYearFilter, setShowPublicationYearFilter] = useState(false);
  const [showPublicationYears, setShowPublicationYears] = useState(false);
  const [showFacultyBreakdown, setShowFacultyBreakdown] = useState(false);
  const [showDocumentTypes, setShowDocumentTypes] = useState(false);
  const [showTopCitedPublications, setShowTopCitedPublications] = useState(false);
  const [dashboardLoading, setDashboardLoading] = useState(true);
  const [researchersLoading, setResearchersLoading] = useState(false);
  const [researchersBuildingMessage, setResearchersBuildingMessage] = useState("");
  const [publicationsLoading, setPublicationsLoading] = useState(false);
  const [fieldsLoading, setFieldsLoading] = useState(false);
  const [error, setError] = useState("");
  const [showResearcherProfile, setShowResearcherProfile] = useState(false);
  const [selectedResearcher, setSelectedResearcher] = useState("");
  const [researcherProfileLoading, setResearcherProfileLoading] = useState(false);
  const [researcherProfileError, setResearcherProfileError] = useState("");
  const [researcherProfile, setResearcherProfile] = useState(null);
  const researcherProfileRequestRef = useRef(0);
  const researcherProfileAbortRef = useRef(null);
  const researcherProfileSearchTimerRef = useRef(null);
  const [profilePublicationTitleQuery, setProfilePublicationTitleQuery] = useState("");
  const dashboardRequestId = useRef(0);
  const pageRequestId = useRef(0);

  const navigateToPage = (pageId) => {
    setSelectedPage(pageId);
    const nextPath = pathForPage(pageId);
    if (window.location.pathname !== nextPath) {
      window.history.pushState({}, "", nextPath);
    }
  };

  const loadDashboard = async (
    faculty = selectedFaculty,
    department = selectedDepartment,
    publicationYears = selectedPublicationYears,
  ) => {
    const requestId = dashboardRequestId.current + 1;
    dashboardRequestId.current = requestId;
    try {
      setDashboardLoading(true);
      setError("");
      const filters = { faculty, department, publicationYears };
      const dashboardPayload = await fetchDashboard(filters);
      if (dashboardRequestId.current === requestId) {
        setDashboard(dashboardPayload);
      }
    } catch (requestError) {
      if (dashboardRequestId.current === requestId) {
        setError(requestError.message);
      }
    } finally {
      if (dashboardRequestId.current === requestId) {
        setDashboardLoading(false);
      }
    }
  };

  useEffect(() => {
    loadDashboard(defaultFaculty, defaultDepartment, []);
  }, []);

  useEffect(() => {
    const handlePopState = () => {
      setSelectedPage(pageFromPath());
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    if (!dashboard) {
      return;
    }
    loadDashboard(selectedFaculty, selectedDepartment, selectedPublicationYears);
  }, [selectedFaculty, selectedDepartment, selectedPublicationYears]);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      setDebouncedResearcherQuery(researcherQuery.trim());
    }, 350);
    return () => window.clearTimeout(timeoutId);
  }, [researcherQuery]);

  useEffect(() => {
    if (!dashboard) {
      return;
    }

    const requestId = pageRequestId.current + 1;
    pageRequestId.current = requestId;
    const filters = {
      faculty: selectedFaculty,
      department: selectedDepartment,
      publicationYears: selectedPublicationYears,
      researcherQuery: debouncedResearcherQuery,
      researcherPage,
      researcherPageSize,
      researcherAlphabet,
      publicationSearch,
      alphabet: publicationAlphabet,
      page: publicationPage,
      pageSize: publicationPageSize,
    };
    const loadPage = async () => {
      try {
        setError("");
        if (selectedPage === "researchers") {
          setResearchersLoading(true);
          setResearchersBuildingMessage("");
          // On a large harvested dataset, clustering every author across every work for the
          // first time (or after a backend restart) can take a few minutes. The backend
          // signals this with an empty result and an X-Researchers-Cache: REBUILDING header
          // instead of erroring, so poll for a while rather than giving up after a few
          // seconds and showing a misleading "No data available" message.
          const maxAttempts = 60;
          const pollIntervalMs = 5000;
          for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
            const { rows: researcherRows, meta: researcherPayloadMeta, cacheStatus } = await fetchResearchers(filters);
            if (pageRequestId.current !== requestId) {
              return;
            }
            const stillBuilding = researcherRows.length === 0 && cacheStatus === "REBUILDING";
            if (!stillBuilding) {
              setResearchers(researcherRows);
              setResearchersMeta({
                total: researcherPayloadMeta?.total ?? researcherRows.length,
                page: researcherPayloadMeta?.page ?? 1,
                page_size: researcherPayloadMeta?.page_size ?? researcherPageSize,
                total_pages: researcherPayloadMeta?.total_pages ?? 1,
              });
              setResearchersLoading(false);
              setResearchersBuildingMessage("");
              break;
            }
            if (attempt === maxAttempts) {
              setResearchers(researcherRows);
              setResearchersMeta({
                total: researcherPayloadMeta?.total ?? researcherRows.length,
                page: researcherPayloadMeta?.page ?? 1,
                page_size: researcherPayloadMeta?.page_size ?? researcherPageSize,
                total_pages: researcherPayloadMeta?.total_pages ?? 1,
              });
              setResearchersLoading(false);
              setResearchersBuildingMessage(
                "Still building the researcher index after several minutes. It will keep " +
                  "running in the background — switch back to this tab in a bit, or reload " +
                  "the page, to check again.",
              );
              break;
            }
            setResearchersBuildingMessage(
              "Building the researcher index for the first time — this can take a few " +
                "minutes on a large dataset. This page will refresh automatically once it's " +
                "ready.",
            );
            await new Promise((resolve) => window.setTimeout(resolve, pollIntervalMs));
          }
        } else if (selectedPage === "publications") {
          setPublicationsLoading(true);
          const publicationPayload = await fetchPublicationsBrowse({ ...filters, ...articleOnlyPublicationFilters });
          if (pageRequestId.current === requestId) {
            setPublications((publicationPayload.rows ?? []).filter(hasVisibleFacultyAndDepartment));
            setPublicationsMeta({
              total: publicationPayload.total ?? 0,
              page: publicationPayload.page ?? 1,
              page_size: publicationPayload.page_size ?? publicationPageSize,
              total_pages: publicationPayload.total_pages ?? 1,
            });
            setPublicationsLoading(false);
          }
        } else if (selectedPage === "fields") {
          setFieldsLoading(true);
          const fieldRows = await fetchResearchFields(filters);
          if (pageRequestId.current === requestId) {
            setResearchFields(fieldRows);
            setFieldsLoading(false);
          }
        }
      } catch (requestError) {
        if (pageRequestId.current === requestId) {
          setError(requestError.message);
          setResearchersLoading(false);
          setPublicationsLoading(false);
          setFieldsLoading(false);
        }
      }
    };

    loadPage();
  }, [
    selectedPage,
    selectedFaculty,
    selectedDepartment,
    selectedPublicationYears,
    debouncedResearcherQuery,
    researcherPage,
    researcherAlphabet,
    publicationAlphabet,
    publicationSearch,
    publicationPage,
    dashboard,
  ]);

  useEffect(() => {
    setResearcherPage(1);
    setPublicationPage(1);
  }, [selectedFaculty, selectedDepartment, selectedPublicationYears, researcherQuery]);

  useEffect(() => {
    setPublicationPage(1);
  }, [publicationAlphabet, publicationSearch]);

  useEffect(() => {
    setResearcherPage(1);
  }, [researcherAlphabet]);

  useEffect(() => () => {
    if (researcherProfileSearchTimerRef.current) {
      clearTimeout(researcherProfileSearchTimerRef.current);
    }
    researcherProfileAbortRef.current?.abort();
  }, []);

  if (dashboardLoading && !dashboard) {
    return (
      <div className="workspace loading">
        <div className="loading-state">
          <span className="loading-spinner" />
          <p className="empty-state">Loading analytics workspace...</p>
        </div>
      </div>
    );
  }

  const availableFaculties = uniqueFilterValues([
    defaultFaculty,
    ...visibleMappingValues(dashboard?.config?.faculties ?? []),
    openAlexUnmappedLabel,
  ]);
  const availableDepartments = uniqueFilterValues([
    defaultDepartment,
    ...visibleMappingValues(dashboard?.config?.departments ?? []),
    openAlexUnmappedLabel,
  ]);
  const availablePublicationYears = Array.from(
    { length: publicationYearFilterEnd - publicationYearFilterStart + 1 },
    (_, index) => publicationYearFilterEnd - index,
  );
  const selectedPublicationYearLabel =
    selectedPublicationYears.length === 0
      ? defaultPublicationYear
      : selectedPublicationYears.slice().sort((a, b) => b - a).join(", ");
  const dashboardScopeLabel =
    selectedPublicationYears.length === 0
      ? "All publication years"
      : `Publication year${selectedPublicationYears.length > 1 ? "s" : ""}: ${selectedPublicationYears
          .slice()
          .sort((a, b) => b - a)
          .join(", ")}`;
  const lastUpdateIso =
    dashboard?.overview?.latest_run?.finished_at ??
    dashboard?.overview?.latest_run?.started_at ??
    null;
  const lastUpdateLabel = lastUpdateIso
    ? new Date(lastUpdateIso).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : "Not available";
  const applyPublicationYears = (years) => {
    const nextYears = years.slice().sort((a, b) => b - a);
    setSelectedPublicationYears(nextYears);
    loadDashboard(selectedFaculty, selectedDepartment, nextYears);
  };
  const togglePublicationYear = (year) => {
    const nextYears = selectedPublicationYears.includes(year)
      ? selectedPublicationYears.filter((selectedYear) => selectedYear !== year)
      : [...selectedPublicationYears, year];
    applyPublicationYears(nextYears);
  };
  const publicationYearCounts = new Map(
    (dashboard?.years ?? []).map((row) => [Number(row.year), row.count]),
  );
  const previewPublicationYears = Array.from(
    { length: publicationYearPreviewEnd - publicationYearPreviewStart + 1 },
    (_, index) => {
      const year = publicationYearPreviewStart + index;
      return { year, count: publicationYearCounts.get(year) ?? 0 };
    },
  );

  const loadResearcherProfile = async (researcher, publicationTitleQuery = "", showLoading = false) => {
    const requestId = researcherProfileRequestRef.current + 1;
    researcherProfileRequestRef.current = requestId;
    researcherProfileAbortRef.current?.abort();
    const abortController = new AbortController();
    researcherProfileAbortRef.current = abortController;
    const filters = {
      faculty: selectedFaculty,
      department: selectedDepartment,
      publicationYears: selectedPublicationYears,
      publicationTitleQuery,
    };
    if (showLoading) {
      setResearcherProfileLoading(true);
    }
    setResearcherProfileError("");
    try {
      const profile = await fetchResearcherProfile(researcher, filters, { signal: abortController.signal });
      if (requestId !== researcherProfileRequestRef.current) {
        return;
      }
      setResearcherProfile(profile);
    } catch (requestError) {
      if (requestError.name === "AbortError") {
        return;
      }
      if (requestId === researcherProfileRequestRef.current) {
        setResearcherProfileError(requestError.message || "Unable to load researcher profile");
      }
    } finally {
      if (showLoading && requestId === researcherProfileRequestRef.current) {
        setResearcherProfileLoading(false);
      }
    }
  };
  const scheduleResearcherPublicationSearch = (nextQuery) => {
    if (researcherProfileSearchTimerRef.current) {
      clearTimeout(researcherProfileSearchTimerRef.current);
    }
    researcherProfileSearchTimerRef.current = setTimeout(() => {
      loadResearcherProfile(selectedResearcher, nextQuery);
    }, 450);
  };
  const openResearcherProfile = async (researcher) => {
    if (researcherProfileSearchTimerRef.current) {
      clearTimeout(researcherProfileSearchTimerRef.current);
    }
    setSelectedResearcher(researcher);
    setShowResearcherProfile(true);
    setResearcherProfile(null);
    setProfilePublicationTitleQuery("");
    await loadResearcherProfile(researcher, "", true);
  };
  const facultyPreviewRows = (dashboard?.facultyBreakdown ?? []).slice(0, 5);
  const documentTypePreviewRows = (dashboard?.documentTypes ?? []).slice(0, 5);
  const topCitedPreviewRows = (dashboard?.topCitedPublications ?? []).slice(0, 5);
  const activeFilters = {
    faculty: selectedFaculty,
    department: selectedDepartment,
    publicationYears: selectedPublicationYears,
  };

  return (
    <div className="workspace">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <p className="eyebrow">University of Pretoria</p>
          <h1>Research Analytics</h1>
        </div>
        <nav className="sidebar-nav">
          {pages.map((page) => (
            <button
              key={page.id}
              className={selectedPage === page.id ? "nav-link active" : "nav-link"}
              onClick={() => navigateToPage(page.id)}
              type="button"
            >
              {page.label}
            </button>
          ))}
        </nav>
      </aside>

      <main className="main-shell">
        <div className="utility-strip">
          <span>Official Digital Service</span>
          <span>University of Pretoria</span>
          <span>Operational controls and analytics</span>
        </div>
        <div className="brand-header">
          <div className="brand-header-left">
            <img src="/up-logo.png" alt="University of Pretoria logo" className="brand-header-logo" />
            <div>
              <p className="eyebrow">Research and Innovation</p>
              <h2>Research Analytics Workspace</h2>
            </div>
          </div>
          <div className="brand-header-right">
            <span>Institutional reporting and insight services</span>
          </div>
        </div>
        <div className="identity-strap">
          <span>Governance, faculty analytics, and publication intelligence</span>
        </div>
        <div className="page-shell">
        <header className="hero compact">
          <div>
            <p className="eyebrow">Control center</p>
            <h2>
              {pages.find((page) => page.id === selectedPage)?.label ?? "Dashboard"}
            </h2>
            <p className="hero-copy">
              Explore publications, researchers, research fields, and source coverage with a
              faculty-aware view of the harvested data.
            </p>
            <section className="hero-panel hero-panel-inline">
              <div className="filter-field filter-field-inline">
                <span>Last Updated</span>
                <div className="filter-static-value filter-static-value-inline">{lastUpdateLabel}</div>
              </div>
            </section>
          </div>
          <div className="hero-panel">
            <label className="filter-field">
              <span>Faculty filter</span>
              <select
                value={selectedFaculty}
                onChange={(event) => {
                  setSelectedFaculty(event.target.value);
                  setSelectedDepartment(defaultDepartment);
                }}
              >
                {availableFaculties.map((faculty) => (
                  <option key={faculty} value={faculty}>
                    {faculty}
                  </option>
                ))}
              </select>
            </label>
            <label className="filter-field">
              <span>Department filter</span>
              <select
                value={selectedDepartment}
                onChange={(event) => setSelectedDepartment(event.target.value)}
              >
                {availableDepartments.map((department) => (
                  <option key={department} value={department}>
                  {department}
                </option>
                ))}
              </select>
            </label>
            <div className="filter-field">
              <span>Publication year</span>
              <div className="checkbox-dropdown">
                <button
                  className="checkbox-dropdown-button"
                  type="button"
                  onClick={() => setShowPublicationYearFilter((isOpen) => !isOpen)}
                  aria-expanded={showPublicationYearFilter}
                >
                  <span>{selectedPublicationYearLabel}</span>
                  <span aria-hidden="true">v</span>
                </button>
                {showPublicationYearFilter ? (
                  <div className="checkbox-dropdown-menu">
                    <label className="checkbox-option">
                      <input
                        type="checkbox"
                        checked={selectedPublicationYears.length === 0}
                        onChange={() => applyPublicationYears([])}
                      />
                      <span>{defaultPublicationYear}</span>
                    </label>
                    {availablePublicationYears.map((year) => (
                      <label className="checkbox-option" key={year}>
                        <input
                          type="checkbox"
                          checked={selectedPublicationYears.includes(year)}
                          onChange={() => togglePublicationYear(year)}
                        />
                        <span>{year}</span>
                      </label>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
            {selectedPage === "publications" ? (
              <label className="filter-field">
                <span>Search publications</span>
                <input
                  type="search"
                  value={publicationSearch}
                  onChange={(event) => setPublicationSearch(event.target.value)}
                  placeholder="Search by publication title"
                />
              </label>
            ) : null}
            {selectedPage === "researchers" ? (
              <label className="filter-field">
                <span>Researcher</span>
                <input
                  type="search"
                  value={researcherQuery}
                  onChange={(event) => setResearcherQuery(event.target.value)}
                  placeholder="Search researcher name"
                />
              </label>
            ) : null}
          </div>
        </header>

        {dashboardLoading && dashboard ? (
          <div className="page-loading-overlay">
            <div className="loading-state">
              <span className="loading-spinner" />
              <span>Updating dashboard...</span>
            </div>
          </div>
        ) : null}

        {error ? <div className="error-banner">{error}</div> : null}

        {selectedPage === "dashboard" ? (
          <>
            <section className="metrics-grid">
              <PieChart
                title="Records by source"
                rows={dashboard?.sources ?? []}
                labelKey="source"
                valueKey="count"
                showLegendValues={false}
                showTotal={false}
                centerLegend
                showChart={false}
              />
              <MetricCard
                label="Total Publications"
                value={dashboard?.overview?.total_works ?? 0}
                detail={`${dashboard?.overview?.selected_faculty ?? defaultFaculty} / ${
                  dashboard?.overview?.selected_department ?? defaultDepartment
                } / ${dashboardScopeLabel}`}
              />
              <MetricCard
                label="Researchers"
                value={dashboard?.overview?.researcher_count ?? 0}
                detail={`Distinct authors in scope / ${dashboardScopeLabel}`}
              />
            </section>

            <section className="kpi-grid">
              <MetricCard
                label="Citations"
                value={dashboard?.kpiSummary?.citations ?? 0}
                detail={`Total citations in the selected scope / ${dashboardScopeLabel}`}
              />
              <MetricCard
                label="h-index"
                value={dashboard?.kpiSummary?.h_index ?? 0}
                detail={`Current h-index for the selected scope / ${dashboardScopeLabel}`}
              />
              <MetricCard
                label="Collaborations"
                value={`${dashboard?.kpiSummary?.collaboration_percentage ?? 0}%`}
                detail={`Share of collaborative publications / ${dashboardScopeLabel}`}
              />
            </section>

            <section className="content-grid content-grid-wide">
              <BarList
                title="Records by faculty"
                rows={facultyPreviewRows}
                labelKey="faculty"
                valueKey="count"
                className="panel-compact"
                headerAction={
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setShowFacultyBreakdown(true)}
                  >
                    View more
                  </button>
                }
              />
              <BarList
                title="Document types"
                rows={documentTypePreviewRows}
                labelKey="document_type"
                valueKey="count"
                className="panel-compact"
                headerAction={
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setShowDocumentTypes(true)}
                  >
                    View more
                  </button>
                }
              />
              <DataTable
                title="Top 10 most cited publications"
                rows={topCitedPreviewRows}
                className="panel-compact"
                headerAction={
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setShowTopCitedPublications(true)}
                  >
                    View more
                  </button>
                }
                columns={[
                  {
                    key: "title",
                    label: "Publication",
                    render: (row) =>
                      row.publication_url ? (
                        <a
                          className="publication-link"
                          href={row.publication_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {row.title}
                        </a>
                      ) : (
                        row.title
                      ),
                  },
                  { key: "publication_year", label: "Year" },
                  { key: "citation_count", label: "Citations" },
                ]}
              />
            </section>

            <section className="single-chart-row">
              <ColumnChart
                title="Publication years"
                rows={previewPublicationYears}
                labelKey="year"
                valueKey="count"
                headerAction={
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setShowPublicationYears(true)}
                  >
                    View more
                  </button>
                }
              />
            </section>

            <section className="page-grid">
              <TopicTrendChart
                title="Topic Trends Over Time"
                data={dashboard?.topicTrends}
              />
            </section>
          </>
        ) : null}

        {selectedPage === "researchers" ? (
          <section className="page-grid">
            {researchersBuildingMessage ? (
              <div className="info-banner">{researchersBuildingMessage}</div>
            ) : null}
            <section className="panel publication-browser-panel">
              <div className="panel-header">
                <div>
                  <h2>Total Researchers</h2>
                </div>
                <strong className="publication-total">{researchersMeta.total.toLocaleString()}</strong>
              </div>
              <div className="alphabet-filter" aria-label="Filter researchers by first letter">
                <button
                  className={`alphabet-button ${researcherAlphabet === "" ? "active" : ""}`.trim()}
                  type="button"
                  onClick={() => setResearcherAlphabet("")}
                >
                  All
                </button>
                {alphabetFilters.map((letter) => (
                  <button
                    className={`alphabet-button ${researcherAlphabet === letter ? "active" : ""}`.trim()}
                    type="button"
                    key={letter}
                    onClick={() => setResearcherAlphabet(letter)}
                  >
                    {letter}
                  </button>
                ))}
              </div>
            </section>
            <DataTable
              title="Researchers"
              rows={researchers}
              loading={researchersLoading}
              columns={[
                {
                  key: "researcher",
                  label: "Researcher",
                  render: (row) => (
                    <button
                      className="researcher-link"
                      type="button"
                      onClick={() => openResearcherProfile(row.researcher)}
                    >
                      {row.researcher}
                    </button>
                  ),
                },
                {
                  key: "faculties",
                  label: "FACULTIES published in",
                  render: (row) => visibleMappingValues(row.faculties || []).join(", "),
                },
                {
                  key: "departments",
                  label: "DEPARTMENTS Published in",
                  render: (row) => visibleMappingValues(row.departments || []).join(", "),
                },
                { key: "publication_count", label: "Publications" },
                { key: "citation_count", label: "Citations" },
                { key: "journal_count", label: "Journals" },
                { key: "latest_year", label: "Latest year" },
              ]}
            />
            <div className="pagination-row">
              <button
                className="secondary-button"
                type="button"
                disabled={researchersLoading || researchersMeta.page <= 1}
                onClick={() => setResearcherPage((currentPage) => Math.max(currentPage - 1, 1))}
              >
                Previous
              </button>
              <span>
                Page {researchersMeta.page.toLocaleString()} of {researchersMeta.total_pages.toLocaleString()}
              </span>
              <button
                className="secondary-button"
                type="button"
                disabled={researchersLoading || researchersMeta.page >= researchersMeta.total_pages}
                onClick={() =>
                  setResearcherPage((currentPage) => Math.min(currentPage + 1, researchersMeta.total_pages))
                }
              >
                Next
              </button>
            </div>
          </section>
        ) : null}

        {selectedPage === "publications" ? (
          <section className="page-grid">
            <section className="panel publication-browser-panel">
              <div className="panel-header">
                <div>
                  <h2>Total Publications</h2>
                </div>
                <strong className="publication-total">{publicationsMeta.total.toLocaleString()}</strong>
              </div>
              <div className="alphabet-filter" aria-label="Filter publications by first letter">
                <button
                  className={`alphabet-button ${publicationAlphabet === "" ? "active" : ""}`.trim()}
                  type="button"
                  onClick={() => setPublicationAlphabet("")}
                >
                  All
                </button>
                {alphabetFilters.map((letter) => (
                  <button
                    className={`alphabet-button ${publicationAlphabet === letter ? "active" : ""}`.trim()}
                    type="button"
                    key={letter}
                    onClick={() => setPublicationAlphabet(letter)}
                  >
                    {letter}
                  </button>
                ))}
              </div>
            </section>
            <DataTable
              title="Publications"
              rows={publications}
              loading={publicationsLoading}
              columns={[
                {
                  key: "title",
                  label: "Title",
                  render: (row) => {
                    const uri = row.uri || row.publication_url;
                    return uri ? (
                      <a
                        className="publication-link"
                        href={uri}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {row.title || "Untitled publication"}
                      </a>
                    ) : (
                      row.title || "Untitled publication"
                    );
                  },
                },
                {
                  key: "faculty",
                  label: "Faculty",
                  render: (row) => visibleMappingValue(row.faculty),
                },
                {
                  key: "department",
                  label: "Department",
                  render: (row) => visibleMappingValue(row.department),
                },
                { key: "document_type", label: "Type" },
                { key: "publication_year", label: "Year" },
                {
                  key: "authors",
                  label: "Authors",
                  render: (row) => {
                    const authors = Array.isArray(row.authors)
                      ? row.authors
                      : String(row.authors || "")
                          .split(";")
                          .map((author) => author.trim())
                          .filter(Boolean);
                    return authors.length ? authors.join("; ") : "Not available";
                  },
                },
              ]}
            />
            <div className="pagination-row">
              <button
                className="secondary-button"
                type="button"
                disabled={publicationsLoading || publicationsMeta.page <= 1}
                onClick={() => setPublicationPage((currentPage) => Math.max(currentPage - 1, 1))}
              >
                Previous
              </button>
              <span>
                Page {publicationsMeta.page.toLocaleString()} of {publicationsMeta.total_pages.toLocaleString()}
              </span>
              <button
                className="secondary-button"
                type="button"
                disabled={publicationsLoading || publicationsMeta.page >= publicationsMeta.total_pages}
                onClick={() =>
                  setPublicationPage((currentPage) => Math.min(currentPage + 1, publicationsMeta.total_pages))
                }
              >
                Next
              </button>
            </div>
          </section>
        ) : null}

        {selectedPage === "fields" ? (
          <section className="page-grid fields-layout">
            <BarList
              title="Research fields"
              rows={researchFields.slice(0, 20)}
              loading={fieldsLoading}
              labelKey="field"
              valueKey="count"
            />
            <DataTable
              title="Research field detail"
              rows={researchFields}
              loading={fieldsLoading}
              columns={[
                { key: "field", label: "Field" },
                { key: "count", label: "Publications" },
              ]}
            />
          </section>
        ) : null}

        {selectedPage === "sdgs" ? (
          <section className="page-grid fields-layout">
            <BarList
              title="SDG keywords"
              rows={(dashboard?.sdgKeywords ?? []).slice(0, 20)}
              loading={dashboardLoading && !dashboard}
              labelKey="keyword"
              valueKey="count"
            />
            <DataTable
              title="SDG keyword detail"
              rows={dashboard?.sdgKeywords ?? []}
              loading={dashboardLoading && !dashboard}
              columns={[
                { key: "keyword", label: "SDG keyword" },
                { key: "count", label: "Publications" },
              ]}
            />
          </section>
        ) : null}

        {showPublicationYears ? (
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={() => setShowPublicationYears(false)}
          >
            <section
              className="modal-panel"
              role="dialog"
              aria-modal="true"
              aria-labelledby="publication-years-title"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-header">
                <h2 id="publication-years-title">All publication years</h2>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Close"
                  onClick={() => setShowPublicationYears(false)}
                >
                  x
                </button>
              </div>
              {(dashboard?.years ?? []).length === 0 ? (
                <p className="empty-state">No data available yet.</p>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Year</th>
                        <th>Publications</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(dashboard?.years ?? []).map((row) => (
                        <tr key={`publication-year-${row.year}`}>
                          <td>{row.year}</td>
                          <td>{row.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        ) : null}
        {showFacultyBreakdown ? (
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={() => setShowFacultyBreakdown(false)}
          >
            <section
              className="modal-panel"
              role="dialog"
              aria-modal="true"
              aria-labelledby="faculty-breakdown-title"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-header">
                <h2 id="faculty-breakdown-title">Records by faculty</h2>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Close"
                  onClick={() => setShowFacultyBreakdown(false)}
                >
                  x
                </button>
              </div>
              <BarList
                title="Records by faculty"
                rows={dashboard?.facultyBreakdown ?? []}
                labelKey="faculty"
                valueKey="count"
              />
            </section>
          </div>
        ) : null}
        {showDocumentTypes ? (
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={() => setShowDocumentTypes(false)}
          >
            <section
              className="modal-panel"
              role="dialog"
              aria-modal="true"
              aria-labelledby="document-types-title"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-header">
                <h2 id="document-types-title">Document types</h2>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Close"
                  onClick={() => setShowDocumentTypes(false)}
                >
                  x
                </button>
              </div>
              <BarList
                title="Document types"
                rows={dashboard?.documentTypes ?? []}
                labelKey="document_type"
                valueKey="count"
              />
            </section>
          </div>
        ) : null}
        {showTopCitedPublications ? (
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={() => setShowTopCitedPublications(false)}
          >
            <section
              className="modal-panel"
              role="dialog"
              aria-modal="true"
              aria-labelledby="top-cited-publications-title"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-header">
                <h2 id="top-cited-publications-title">Top 10 most cited publications</h2>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Close"
                  onClick={() => setShowTopCitedPublications(false)}
                >
                  x
                </button>
              </div>
              <DataTable
                title="Top 10 most cited publications"
                rows={dashboard?.topCitedPublications ?? []}
                columns={[
                  {
                    key: "title",
                    label: "Publication",
                    render: (row) =>
                      row.publication_url ? (
                        <a
                          className="publication-link"
                          href={row.publication_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {row.title}
                        </a>
                      ) : (
                        row.title
                      ),
                  },
                  { key: "publication_year", label: "Year" },
                  { key: "citation_count", label: "Citations" },
                ]}
              />
            </section>
          </div>
        ) : null}
        {showResearcherProfile ? (
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={() => setShowResearcherProfile(false)}
          >
            <section
              className="modal-panel modal-panel-wide researcher-profile-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="researcher-profile-title"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-header">
                <h2 id="researcher-profile-title">Researcher profile</h2>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Close"
                  onClick={() => setShowResearcherProfile(false)}
                >
                  x
                </button>
              </div>
              {researcherProfileLoading ? (
                <div className="loading-state">
                  <span className="loading-spinner" />
                  <span>Loading researcher profile...</span>
                </div>
              ) : researcherProfileError ? (
                <div className="error-banner">{researcherProfileError}</div>
              ) : researcherProfile ? (
                <div className="researcher-modal-content">
                  <section className="panel">
                    <div className="panel-header">
                      <h2>{researcherProfile.researcher || selectedResearcher}</h2>
                    </div>
                    <div className="profile-summary-grid">
                      <div>
                        <p className="summary-label">Affiliation (institution)</p>
                        <p className="summary-value">
                          {(researcherProfile.affiliations || [])
                            .slice(0, 3)
                            .map((item) => item.institution)
                            .join(", ") || "Not available"}
                        </p>
                      </div>
                      <div>
                        <p className="summary-label">ORCID (if available)</p>
                        <p className="summary-value">{researcherProfile.orcid || "Not available"}</p>
                      </div>
                    </div>
                    <div className="kpi-grid">
                      <MetricCard
                        label="Publications"
                        value={researcherProfile.quick_stats?.publications ?? 0}
                        detail="Quick stats"
                      />
                      <MetricCard
                        label="Citations"
                        value={researcherProfile.quick_stats?.citations ?? 0}
                        detail="Quick stats"
                      />
                      <MetricCard
                        label="h-index"
                        value={researcherProfile.quick_stats?.h_index ?? 0}
                        detail="Quick stats"
                      />
                    </div>
                  </section>

                  <section className="panel">
                    <div className="panel-header">
                      <h2>1. Performance Overview</h2>
                    </div>
                    <div className="profile-charts-grid">
                      <ColumnChart
                        title="Publications over time (yearly publication output)"
                        rows={researcherProfile.performance_overview?.publications_over_time ?? []}
                        labelKey="year"
                        valueKey="count"
                      />
                      <ColumnChart
                        title="Citations over time (yearly citation impact)"
                        rows={researcherProfile.performance_overview?.citations_over_time ?? []}
                        labelKey="year"
                        valueKey="count"
                      />
                    </div>
                  </section>

                  <section className="panel">
                    <div className="panel-header">
                      <h2>2. Publications</h2>
                    </div>
                    <label className="filter-field profile-publication-filter">
                      <span>Publication title</span>
                      <input
                        type="search"
                        value={profilePublicationTitleQuery}
                        onChange={(event) => {
                          const nextQuery = event.target.value;
                          setProfilePublicationTitleQuery(nextQuery);
                          scheduleResearcherPublicationSearch(nextQuery);
                        }}
                        placeholder="Search publication title"
                      />
                    </label>
                    <DataTable
                      title="Recent and top publications"
                      rows={researcherProfile.publications ?? []}
                      columns={[
                        {
                          key: "title",
                          label: "Title",
                          render: (row) => {
                            const uri = row.uri || row.publication_url || row.url;
                            return uri ? (
                              <a className="publication-link" href={uri} target="_blank" rel="noreferrer">
                                {row.title}
                              </a>
                            ) : (
                              row.title
                            );
                          },
                        },
                        { key: "year", label: "Year" },
                        { key: "source", label: "Source" },
                        { key: "open_access_status", label: "Open Access status" },
                        { key: "citations", label: "Citations" },
                      ]}
                    />
                  </section>

                  <section className="panel">
                    <div className="panel-header">
                      <h2>3. Collaboration</h2>
                    </div>
                    <div className="profile-collab-grid">
                      <BarList
                        title="Co-authors by publication count"
                        rows={researcherProfile.collaboration?.coauthors ?? []}
                        labelKey="name"
                        valueKey="count"
                      />
                      <BarList
                        title="Countries/institutions by collaboration count"
                        rows={researcherProfile.collaboration?.institutions ?? []}
                        labelKey="institution"
                        valueKey="count"
                      />
                    </div>
                  </section>

                  <section className="panel">
                    <div className="panel-header">
                      <h2>4. Research Areas</h2>
                    </div>
                    <div className="profile-collab-grid">
                      <BarList
                        title="Keywords/topics by frequency"
                        rows={researcherProfile.research_areas?.keywords ?? []}
                        labelKey="keyword"
                        valueKey="count"
                      />
                      <BarList
                        title="Subject areas by frequency"
                        rows={researcherProfile.research_areas?.subject_areas ?? []}
                        labelKey="subject_area"
                        valueKey="count"
                      />
                    </div>
                  </section>
                </div>
              ) : (
                <p className="empty-state">No data available for this researcher.</p>
              )}
            </section>
          </div>
        ) : null}
        </div>
      </main>
    </div>
  );
}
