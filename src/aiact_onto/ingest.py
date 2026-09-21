from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


HTML = "http://publications.europa.eu/resource/celex/32024R1689"


HEADERS = {
    "Accept": "application/xhtml+xml, text/html;q=0.9",
    "Accept-Language": "eng",
}


class Ingest:
    def __init__(self, html: str = HTML):
        self.html = html
        self.path = "./data/raw/"

    def crawl(self) -> str:
        """Ingests the HTML content from the specified URL.
        
        Returns:
            str: The ingested HTML content.
        """

        response = requests.get(self.html, headers=HEADERS, timeout=60)
        response.raise_for_status()

        if response.headers.get("x-amzn-waf-action") or response.status_code == 202:
            raise RuntimeError(
                f"{self.html} returned a bot-protection challenge instead of the document"
            )
        response.encoding = "utf-8"
        return response.text

    def save(self, content: str, filename: str = "output.html") -> None:
        """Saves the ingested HTML content to a file.
        
        Args:
            content (str): The ingested HTML content.
            filename (str): The name of the file to save the content to.
        """
        with open(self.path + filename, "w", encoding="utf-8") as file:
            file.write(content)

        with open(self.path + "last_ingest.txt", "w", encoding="utf-8") as file:
            file.write(datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def load(self, filename: str = "output.html") -> str:
        """Loads the ingested HTML content from a file.

        Args:
            filename (str): The name of the file to load the content from.
        """
        with open(self.path + filename, "r", encoding="utf-8") as file:
            content = file.read()
        return content

    def last_ingest(self) -> datetime | None:
        """Returns the timestamp of the last successful ingest.

        Returns:
            datetime | None: The UTC timestamp of the last successful ingest,
                or None if no previous ingest was found or the file is unreadable.
        """
        try:
            with open(self.path + "last_ingest.txt", "r", encoding="utf-8") as file:
                return datetime.fromisoformat(file.read().strip())
        except (FileNotFoundError, ValueError):
            return None

    def should_crawl(self) -> bool:
        """Determines whether to crawl based on the last ingest timestamp.
        
        Returns:
            bool: True if the last ingest was more than 24 hours ago or if no previous ingest was found, 
                False otherwise.
        """
        last_ingest_time = self.last_ingest()
        html_file = Path(self.path) / "output.html"
        if last_ingest_time is None or not html_file.is_file() or html_file.stat().st_size == 0:
            return True

        return datetime.now(timezone.utc) - last_ingest_time > timedelta(days=1)

    def ingest(self) -> str:
        """Crawls the HTML content and saves it to a file.
        
        Returns:
            str: The ingested HTML content.
        """
        html_content = ""
        if self.should_crawl():
            html_content = self.crawl()
            self.save(html_content)
        else:
            html_content = self.load()
        return html_content


if __name__ == "__main__":
    ingest_instance = Ingest()
    html_content = ingest_instance.ingest()
    print(html_content)
