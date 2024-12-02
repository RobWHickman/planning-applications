import json
import logging
from datetime import date, datetime
from enum import Enum
from typing import Generator, List, Optional

import scrapy
from scrapy.http import HtmlResponse, TextResponse

from planning_applications.items import (
    PlanningApplicationDetailsFurtherInformation,
    PlanningApplicationDetailsSummary,
    PlanningApplicationItem,
    PlanningApplicationPolygon,
)
from planning_applications.settings import DEFAULT_DATE_FORMAT
from planning_applications.spiders.base import BaseSpider

logging.getLogger().setLevel(logging.WARNING)

DEFAULT_START_DATE = datetime.fromisocalendar(datetime.now().year, 1, 1).date()
DEFAULT_END_DATE = datetime.now().date()


class applicationStatus(Enum):
    ALL = ""
    APPEAL_DECIDED = "Appeal decided"
    APPEAL_LODGED = "Appeal lodged"
    AWAITING_DECISION = "Awaiting decision"
    DECIDED = "Decided"
    REGISTERED = "Registered"
    UNKNOWN = "Unknown"
    WITHDRAWN = "Withdrawn"


class IdoxSpider(BaseSpider):
    start_url: str
    base_url: str
    allowed_domains: List[str] = []
    arcgis_url: Optional[str] = None

    # Date to start searching from, format: YYYY-MM-DD. Default: 1st January of the current year
    start_date: date = "2024-11-01"
    # Date to stop searching at, format: YYYY-MM-DD. Default: today
    end_date: date = DEFAULT_END_DATE
    filter_status: applicationStatus = applicationStatus.ALL

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if isinstance(self.start_date, str):
            self.start_date = datetime.strptime(self.start_date, DEFAULT_DATE_FORMAT).date()

        if isinstance(self.end_date, str):
            self.end_date = datetime.strptime(self.end_date, DEFAULT_DATE_FORMAT).date()

        if isinstance(self.filter_status, str):
            self.filter_status = applicationStatus(self.filter_status)

        if self.start_date > self.end_date:
            raise ValueError(f"start_date {self.start_date} must be earlier than to_date {self.end_date}")

    def start_requests(self):
        self.logger.info(
            f"Searching for {self.name} applications between {self.start_date} and {self.end_date} with status {self.filter_status == applicationStatus.ALL and 'all' or self.filter_status.value}"
        )
        yield scrapy.Request(self.start_url, callback=self.submit_form)

    def submit_form(self, response):
        self.logger.info(f"Submitting search form on {response.url}")

        formdata = {
            "_csrf": response.css("input[name='_csrf']::attr(value)").get(),
            "caseAddressType": "Application",
            "date(applicationValidatedStart)": self.formatted_start_date,
            "date(applicationValidatedEnd)": self.formatted_end_date,
            "searchType": "Application",
        }

        if self.filter_status != applicationStatus.ALL:
            formdata["caseStatus"] = self.filter_status.value

        return [scrapy.FormRequest.from_response(response, formdata=formdata, callback=self.parse_results)]

    def parse_results(self, response: HtmlResponse):
        message_box = response.css(".messagebox")

        if len(message_box) > 0:
            if "No results found" in message_box[0].extract():
                self.logger.info(f"No applications found on {response.url}")
                return

            if "Too many results found" in message_box[0].extract():
                self.logger.error(f"Too many results found on {response.url}. Make the search more specific.")
                return

        application_tools = response.css("#applicationTools")
        if len(application_tools) > 0:
            self.logger.info(f"Only one application found on {response.url}")
            yield from self.parse_details_summary_tab(response)
            return

        search_results = response.css("#searchresults")
        if len(search_results) == 0:
            self.logger.info(f"No applications found on {response.url}")
            return

        search_results = search_results[0].css(".searchresult")
        self.logger.info(f"Found {len(search_results)} applications on {response.url}")

        for result in search_results:
            self.logger.info(f"heading into {result.css("a::attr(href)").get()}")
            if self.applications_scraped >= self.limit:
                self.logger.info(f"Reached the limit of {self.limit} applications")
                return

            yield from self._parse_single_result(result, response)

        next_page = response.css(".next::attr(href)").get()
        if next_page:
            self.logger.info(f"Found next page at {next_page}")
            next_page_url = response.urljoin(next_page)
            yield scrapy.Request(next_page_url, callback=self.parse_results)

    def _parse_single_result(self, result: scrapy.Selector, response: HtmlResponse):
        details_summary_url = self._get_single_result_details_summary_url(result, response)
        details_further_information_url = self._get_single_result_details_further_information_url(result, response)

        keyval = details_summary_url.split("keyVal=")[1].split("&")[0] or ""
        if keyval == "":
            self.logger.error(f"Failed to parse keyval from {details_summary_url}, can't continue")
            return

        if self.should_scrape_application:
            self.applications_scraped += 1

            meta = {
                "keyval": keyval,
                "original_response": response,
                "limit": self.limit,
                "applications_scraped": self.applications_scraped,
            }

            yield scrapy.Request(
                details_summary_url,
                callback=self.parse_details_summary_tab,
                meta=meta,
                errback=self.handle_error,
            )

            yield scrapy.Request(
                details_further_information_url,
                callback=self.parse_details_further_information_tab,
                meta=meta,
                errback=self.handle_error,
            )

            # if self.should_scrape_document:
            #     yield scrapy.Request(
            #         details_summary_url.replace("activeTab=summary", "activeTab=documents"),
            #         callback=self.parse_documents_tab,
            #         meta={"application_reference": details_summary.reference},
            #     )

            # if self.should_scrape_comment:
            #     # TODO: Implement comment scraping
            #     pass

            # self.logger.info(f"Scraping ArcGIS data for {keyval}")

            # if self.should_scrape_polygon and self.arcgis_url:
            #     url = (
            #         self.arcgis_url
            #         + "?f=geojson&returnGeometry=true&outFields=*&outSR=4326&where=KEYVAL%3D%27"
            #         + keyval
            #         + "%27"
            #     )

            # yield scrapy.Request(
            #     url,
            #     callback=self.parse_idox_arcgis,
            #     meta={"application_reference": details_summary.reference, "keyval": keyval},
            # )

    # Details
    # -------------------------------------------------------------------------

    def _get_single_result_details_summary_url(self, result: scrapy.Selector, response: HtmlResponse) -> str:
        return response.urljoin(result.css("a::attr(href)").get())

    def parse_details_summary_tab(
        self, response: HtmlResponse
    ) -> Generator[PlanningApplicationDetailsSummary, None, None]:
        self.logger.info(f"Parsing results on {response.url} (parse_details_summary_tab)")

        details_summary = PlanningApplicationDetailsSummary()

        summary_table = response.css("#simpleDetailsTable")[0]

        details_summary.reference = self._get_horizontal_table_value(summary_table, "Reference")

        application_received = self._get_horizontal_table_value(summary_table, "Application Received")
        if application_received:
            details_summary.application_received = datetime.strptime(application_received, "%a %d %b %Y")

        application_validated = self._get_horizontal_table_value(summary_table, "Application Validated")
        if application_validated:
            details_summary.application_validated = datetime.strptime(application_validated, "%a %d %b %Y")

        details_summary.address = self._get_horizontal_table_value(summary_table, "Address")
        details_summary.proposal = self._get_horizontal_table_value(summary_table, "Proposal")
        details_summary.appeal_status = self._get_horizontal_table_value(summary_table, "Appeal Status")
        details_summary.appeal_decision = self._get_horizontal_table_value(summary_table, "Appeal Decision")

        meta = response.meta
        meta["details_summary"] = details_summary

        if "details_further_information" in meta:
            yield self.create_planning_application_item(meta)
        else:
            yield scrapy.Request(
                response.url,
                callback=self.parse_details_further_information_tab,
                meta=meta,
                errback=self.handle_error,
            )

    def _get_single_result_details_further_information_url(
        self, result: scrapy.Selector, response: HtmlResponse
    ) -> str:
        return self._get_single_result_details_summary_url(result, response).replace(
            "activeTab=summary", "activeTab=details"
        )

    def parse_details_further_information_tab(
        self, response: HtmlResponse
    ) -> Generator[PlanningApplicationDetailsFurtherInformation, None, None]:
        self.logger.info(f"Parsing results on {response.url} (parse_details_further_information_tab)")

        details_table = response.css("#applicationDetails")[0]

        details_further_information = PlanningApplicationDetailsFurtherInformation()

        details_further_information.application_type = self._get_horizontal_table_value(
            details_table, "Application Type"
        )
        details_further_information.expected_decision_level = self._get_horizontal_table_value(
            details_table, "Expected Decision Level"
        )
        details_further_information.case_officer = self._get_horizontal_table_value(details_table, "Case Officer")
        details_further_information.parish = self._get_horizontal_table_value(details_table, "Parish")
        details_further_information.ward = self._get_horizontal_table_value(details_table, "Ward")
        details_further_information.applicant_name = self._get_horizontal_table_value(details_table, "Applicant Name")
        details_further_information.district_reference = self._get_horizontal_table_value(
            details_table, "District Reference"
        )
        details_further_information.applicant_name = self._get_horizontal_table_value(details_table, "Applicant Name")
        details_further_information.applicant_address = self._get_horizontal_table_value(
            details_table, "Applicant Address"
        )
        details_further_information.environmental_assessment_requested = self._get_horizontal_table_value(
            details_table, "Environmental Assessment Requested"
        )

        meta = response.meta
        meta["details_further_information"] = details_further_information

        if "details_summary" in meta:
            yield self.create_planning_application_item(meta, response)
        else:
            yield scrapy.Request(
                response.url,
                callback=self.parse_details_summary_tab,
                meta=meta,
                errback=self.handle_error,
            )

    # Comments
    # -------------------------------------------------------------------------

    def _get_single_result_comments_public_url(self, result: scrapy.Selector, response: HtmlResponse):
        return self._get_single_result_details_summary_url(result, response).replace(
            "activeTab=summary", "activeTab=neighbourComments"
        )

    def _get_single_result_comments_consultee_url(self, result: scrapy.Selector, response: HtmlResponse):
        return self._get_single_result_details_summary_url(result, response).replace(
            "activeTab=summary", "activeTab=consulteeComments"
        )

    # Documents
    # -------------------------------------------------------------------------

    def _get_single_result_documents_url(self, result: scrapy.Selector, response: HtmlResponse):
        return self._get_single_result_details_summary_url(result, response).replace(
            "activeTab=summary", "activeTab=documents"
        )

    # Related Cases
    # -------------------------------------------------------------------------

    def _get_single_result_related_cases_url(self, result: scrapy.Selector, response: HtmlResponse):
        return self._get_single_result_details_summary_url(result, response).replace(
            "activeTab=summary", "activeTab=relatedcases"
        )

    # Map
    # -------------------------------------------------------------------------

    def _get_single_result_map_url(self, result: scrapy.Selector, response: HtmlResponse):
        return self._get_single_result_details_summary_url(result, response).replace(
            "activeTab=summary", "activeTab=map"
        )

    # -------------------------------------------------------------------------

    # def parse_documents_tab(self, response: HtmlResponse):
    #     self.logger.info(f"Parsing documents on {response.url}")

    #     table = response.css("#Documents")[0]
    #     rows = table.xpath(".//tr")[1:]

    #     self.logger.info(f"Found {len(rows)} documents on {response.url}")

    #     for row in rows:
    #         yield from self._parse_document_row(table, row, response)

    # PARSE_DOCUMENT_ROW_COLUMN_HEADERS = {
    #     "date": "Date Published",
    #     "category": "Document Type",
    #     "description": "Description",
    #     "document_reference": "Drawing Number",
    #     "view_link": "View",
    # }

    # def _parse_document_row(self, table: scrapy.Selector, row: scrapy.Selector, response: HtmlResponse):
    #     date_cell = get_cell_for_column_name(table, row, self.PARSE_DOCUMENT_ROW_COLUMN_HEADERS["date"])
    #     category_cell = get_cell_for_column_name(table, row, self.PARSE_DOCUMENT_ROW_COLUMN_HEADERS["category"])
    #     description_cell = get_cell_for_column_name(table, row, self.PARSE_DOCUMENT_ROW_COLUMN_HEADERS["description"])
    #     view_link_cell = get_cell_for_column_name(table, row, self.PARSE_DOCUMENT_ROW_COLUMN_HEADERS["view_link"])

    #     datestr = date_cell.xpath("./text()").get()
    #     if not datestr:
    #         self.logger.error(f"Failed to parse date from row {row}, can't continue")
    #         return

    #     date = datetime.strptime(datestr, "%d %b %Y").strftime("%Y-%m-%d")
    #     category = category_cell.xpath("./text()").get()
    #     description = description_cell.xpath("./text()").get()
    #     url = response.urljoin(view_link_cell.xpath("./a/@href").get())

    #     yield scrapy.Request(
    #         url,
    #         callback=self.process_parsed_file,
    #         meta={
    #             "original_response": response,
    #             "date": date,
    #             "category": category,
    #             "description": description,
    #             "url": url,
    #         },
    #         cookies=cast(dict, response.headers.getlist("Set-Cookie")),
    #     )

    def _get_horizontal_table_value(self, table: scrapy.Selector, column_name: str):
        texts = table.xpath(f".//th[contains(text(), '{column_name}')]/following-sibling::td/text()").get()
        if texts:
            return "".join(texts).strip()
        return None

    # def process_parsed_file(self, response: HtmlResponse):
    #     # move this definition higher up to minimise no. of repeated calls

    #     # TODO: Implement this
    #     parsed_data = ""

    #     for item in parsed_data:
    #         return self._create_planning_application_document(item, response)

    # def _create_planning_application_document(
    #     self, item, response: HtmlResponse
    # ) -> Generator[PlanningApplicationDocument, None, None]:
    #     # Some of these document fields aren't included in the standard document definition
    #     # (application_reference, category, description, date)
    #     # They have been passed into storage via the metadata field instead
    #     metadata = json.loads(item["metadata"])
    #     metadata["application_reference"] = response.meta["original_response"].meta["application_reference"]
    #     metadata["category"] = response.meta["category"]
    #     metadata["description"] = response.meta["description"]
    #     metadata["date"] = response.meta["date"]

    #     yield PlanningApplicationDocument(
    #         meta_source_url=response.meta["original_response"].url,
    #         planning_application_reference=metadata["application_reference"],
    #         content_hash=item["content_hash"],
    #         file_name=item["file_name"],
    #         url=response.meta["url"],
    #         metadata=json.dumps(metadata),
    #         lpa=self.name,
    #         mimetype=item["mimetype"],
    #         body=item["body"],
    #     )

    # Related Cases
    # -------------------------------------------------------------------------

    def parse_related_cases_tab(self, response: HtmlResponse):
        pass

    # ArcGIS / Map
    # -------------------------------------------------------------------------

    def parse_idox_arcgis(self, response: TextResponse) -> Generator[PlanningApplicationPolygon, None, None]:
        parsed_response = json.loads(response.text)

        if parsed_response["features"] is None:
            self.logger.error(f"No features found in response from {response.url}")
            return

        if len(parsed_response["features"]) == 0:
            self.logger.error(f"No features found in response from {response.url}")
            return

        if parsed_response["features"][0]["geometry"] is None:
            self.logger.error(f"No geometry found in response from {response.url}")
            return

        if parsed_response["features"][0]["properties"] is None:
            self.logger.error(f"No geometry found in response from {response.url}")
            return

        if parsed_response["features"][0]["properties"]["KEYVAL"] is None:
            self.logger.error(f"No KEYVAL found in response from {response.url}")
            return

        if parsed_response["features"][0]["properties"]["KEYVAL"] != response.meta["keyval"]:
            self.logger.error(f"KEYVAL mismatch in response from {response.url}")
            return

        yield PlanningApplicationPolygon(
            meta_source_url=response.url,
            lpa=self.name,
            reference=response.meta["application_reference"],
            polygon_geojson=json.dumps(parsed_response["features"][0]),
        )

    # Helpers
    # -------------------------------------------------------------------------

    @property
    def formatted_start_date(self) -> str:
        return self.start_date.strftime("%d/%m/%Y")

    @property
    def formatted_end_date(self) -> str:
        return self.end_date.strftime("%d/%m/%Y")

    def create_planning_application_item(
        self, meta, response: HtmlResponse
    ) -> Generator[PlanningApplicationItem, None, None]:
        details_summary = meta["details_summary"]
        details_further_information = meta["details_further_information"]

        item = PlanningApplicationItem(
            lpa=self.name,
            reference=details_summary.reference,
            application_received=details_summary.application_received,
            application_validated=details_summary.application_validated,
            address=details_summary.address,
            proposal=details_summary.proposal,
            appeal_status=details_summary.appeal_status,
            appeal_decision=details_summary.appeal_decision,
            application_type=details_further_information.application_type,
            expected_decision_level=details_further_information.expected_decision_level,
            case_officer=details_further_information.case_officer,
            parish=details_further_information.parish,
            ward=details_further_information.ward,
            district_reference=details_further_information.district_reference,
            applicant_name=details_further_information.applicant_name,
            applicant_address=details_further_information.applicant_address,
            environmental_assessment_requested=details_further_information.environmental_assessment_requested,
        )

        # Handle document scraping if needed
        # if self.should_scrape_document:
        #     documents_url = self._get_single_result_documents_url(meta["original_response"], response)
        #     yield scrapy.Request(
        #         documents_url,
        #         callback=self.parse_documents_tab,
        #         meta={"application_reference": details_summary.reference},
        #     )

        # Similarly, handle other scraping needs like comments, polygons, etc.

        yield item

    def handle_error(self, failure):
        self.logger.error(f"Error processing request {failure.request}")
        self.logger.error(f"Error details: {failure.value}")


def get_cell_for_column_name(table: scrapy.Selector, row: scrapy.Selector, column_name: str) -> scrapy.Selector:
    try:
        column_index = int(
            float(table.css(f"th:contains('{column_name}')").xpath("count(preceding-sibling::th)").get())
        )
    except ValueError:
        raise ValueError(f"Column '{column_name}' not found in table")

    return row.xpath(f"./td[{column_index + 1}]")