"""Ссылки карточки на Ahrefs Site Explorer (E9-13).

Настройки отчётов — из ссылок пользователя 08.10.2026; меняется только
целевой домен. Никаких запросов к Ahrefs при показе карточки не выполняем.
"""

from urllib.parse import urlencode

_OVERVIEW: dict[str, str] = {
    "backlinksChartMode": "metrics",
    "backlinksChartPerformanceSources": "domainRating||urlRating",
    "backlinksCompetitorsSource": '"UrlRating"',
    "backlinksRefdomainsSource": '"RefDomainsNew"',
    "bestFilter": "all",
    "brandedTrafficChartMetric": "organic-traffic",
    "brandedTrafficSource": "target-brand",
    "chartGranularity": "daily",
    "chartInterval": "year2",
    "competitors": "",
    "competitorsSource": "custom",
    "countries": "",
    "country": "all",
    "dataMode": "text",
    "entitiesCategory": "organisations",
    "generalChartBrandedTraffic": "non-branded||other-brands||target-brand",
    "generalChartMode": "metrics",
    "generalChartPerformanceSources": "domainRating||organicTraffic||paidTraffic||refDomains",
    "generalCompetitorsSource": '"OrganicTraffic"',
    "generalCountriesSource": "organic-traffic",
    "generalEntitiesChartMetric": "Traffic",
    "generalPagesByTrafficChartMode": "Percentage",
    "generalPagesByTrafficSource": "Pages||Traffic",
    "generalPagesByTypeSource": "Pages||Traffic",
    "highlightChanges": "1m",
    "intentsMainSource": "informational",
    "keywordsSource": "all",
    "mode": "subdomains",
    "organicChartBrandedTraffic": "non-branded||other-brands||target-brand",
    "organicChartMode": "metrics",
    "organicChartPerformanceSources": "impressions||organicTraffic||organicTrafficValue",
    "organicCompetitorsSource": '"OrganicTraffic"',
    "organicCountriesSource": "organic-traffic",
    "organicEntitiesChartMetric": "Traffic",
    "organicPagesByTrafficChartMode": "Percentage",
    "organicPagesByTrafficSource": "Pages||Traffic",
    "organicPagesByTypeSource": "Pages||Traffic",
    "overviewSerpChartMode": "Own",
    "overviewSerpChartSpec": "||".join(
        (
            "AIOverview",
            "AdwordsBottom",
            "AdwordsTop",
            "Discussions",
            "FeaturedSnippet",
            "ImagePack",
            "KnowledgeCard",
            "KnowledgePanel",
            "LocalPack",
            "PaidSiteLinks",
            "PeopleAlsoAsk",
            "ShoppingAds",
            "ShoppingOrganic",
            "Sitelinks",
            "Thumbnail",
            "TopStories",
            "Tweets",
            "VideoPreview",
            "Videos",
        )
    ),
    "overviewSerpManyChartSpec": "Own||Total",
    "overview_tab": "general",
    "paidSearchPaidKeywordsByTopPositionsChartMode": "Percentage",
    "paidTrafficSources": "cost||traffic",
    "topLevelDomainFilter": "all",
    "topOrganicKeywordsMode": "normal",
    "topOrganicPagesMode": "normal",
    "trafficType": "Organic",
    "volume_type": "monthly",
}

_TOP_PAGES: dict[str, str] = {
    "chartGranularity": "daily",
    "chartInterval": "year5",
    "chartMode": "perf",
    "compareDate": "prevMonth",
    "competitorsSource": "traffic",
    "country": "all",
    "currentDate": "today",
    "dataMode": "text",
    "hiddenColumns": "",
    "keywordRules": "",
    "limit": "50",
    "mode": "subdomains",
    "offset": "0",
    "orgKwdChanges": "",
    "orgTrafficChanges": "",
    "orgValueChanges": "",
    "pageStatus": "",
    "positionChanges": "",
    "serpTitleRules": "",
    "showTitles": "0",
    "sort": "TrafficInitial",
    "sortDirection": "desc",
    "urlRules": "",
    "volume_type": "monthly",
}


def report_links(domain: str) -> dict[str, str]:
    """Overview и Top pages: весь домен с поддоменами, все страны."""
    base = "https://app.ahrefs.com/site-explorer"
    return {
        "overview": f"{base}/overview?{urlencode({**_OVERVIEW, 'target': domain})}",
        "top_pages": f"{base}/top-pages?{urlencode({**_TOP_PAGES, 'target': domain})}",
    }
