import logging
import urllib.parse
import urllib.request
import os
import shutil
import glob
import hashlib
import mimetypes
import re
import sys
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from selenium import webdriver
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup, Tag
import html5lib  # used by bs4
import requests
import cssutils
from appdirs import user_cache_dir

from logger import LOG, trace
from driver import DriverInitializer
from argparser import ArgParser


class FileManager:
    @trace()
    def __init__(self, url: str, cache_assets: bool = True) -> None:
        self.url = url
        id = urllib.parse.urlparse(self.url).path[1:]
        name = id[: id.rfind("-")].lower()
        self.output_dir = os.path.join("snapshots", name)
        self.cache_assets = cache_assets
        cache_base_dir = user_cache_dir(appname="notion-snapshot", appauthor="suezli")
        self.cache_dir = os.path.join(cache_base_dir, name)

        self._setup()

    @trace()
    def _setup(self) -> None:
        if os.path.exists(self.output_dir):
            if self.cache_assets and os.path.exists(self.output_dir + "/assets"):
                LOG.info("Copying assets to caching directory")
                shutil.copytree(self.output_dir + "/assets", self.cache_dir, dirs_exist_ok=True)

            shutil.rmtree(self.output_dir)
            LOG.info(f"removed previous snapshot for this url")

        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.output_dir + "/assets", exist_ok=True)
        if self.cache_assets:
            os.makedirs(self.cache_dir, exist_ok=True)

    @trace()
    def download_asset(self, url: str, filename: str = "") -> str:
        if not filename:
            parsed_url = urllib.parse.urlparse(url)
            queryless_url = parsed_url.netloc + parsed_url.path
            params = urllib.parse.parse_qs(parsed_url.query)
            # having width in hash as name allows fetching different image resolutions
            if "width" in params.keys():
                queryless_url = queryless_url + f"?width={params['width']}"
            filename = hashlib.sha1(str.encode(queryless_url)).hexdigest()
            LOG.info("no filename, generated hash: " + filename)

        already_downloaded = glob.glob(self.output_dir + "/assets/" + filename + ".*")
        if already_downloaded:
            return str(Path(already_downloaded[0]).relative_to(self.output_dir)).replace("\\", "/")

        if self.cache_assets and (cached := self._load_from_cache(filename)) is not None:
            return cached

        destination = Path(self.output_dir) / "assets" / filename
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.get(url)
            response.raise_for_status()

            missing_file_extension = not bool(destination.suffix)
            if missing_file_extension:
                suffix = Path(urllib.parse.urlparse(url).path).suffix
                question_mark = "%3f"
                if suffix:
                    if question_mark in suffix.lower():
                        suffix = re.split(question_mark, suffix, flags=re.IGNORECASE)[0]
                    destination = destination.with_suffix(suffix)
                else:
                    content_type = response.headers.get("content-type")
                    assert content_type is not None
                    mimetype = mimetypes.guess_extension(content_type)
                    assert mimetype is not None
                    destination = destination.with_suffix(mimetype)

            with open(destination, "wb") as f:
                f.write(response.content)
            return str(destination.relative_to(self.output_dir)).replace("\\", "/")

        except Exception as error:
            LOG.warning(f"error downloading asset on '{url}' - a hyperlink will be used in snapshot instead \n {error}")
            return str(Path(url)).replace("\\", "/")

    def _load_from_cache(self, filename: str) -> Optional[str]:
        filename = filename if Path(filename).suffix else filename + ".*"
        cache_path = os.path.join(self.cache_dir, filename)
        cached = glob.glob(cache_path)
        LOG.debug("cached: " + str(cached))
        if cached:
            LOG.debug(f"File {filename} found in cache. Using cached version")
            shutil.copy(cached[0], self.output_dir + "/assets")
            return os.path.join("assets", os.path.basename(cached[0]))
        return None

    @trace()
    def copy_injections_to_assets(self) -> Tuple[str, str]:
        injection_dir = Path(__file__).parent / "injections"
        css_out = Path(self.output_dir) / "assets" / "injection.css"
        js_out = Path(self.output_dir) / "assets" / "injection.js"
        shutil.copy(injection_dir / "injection.js", js_out)
        shutil.copy(injection_dir / "injection.css", css_out)
        relative_css_out = str(css_out.relative_to(self.output_dir)).replace("\\", "/")
        relative_js_out = str(js_out.relative_to(self.output_dir)).replace("\\", "/")
        return relative_css_out, relative_js_out

    @trace()
    def save_page(self, soup: BeautifulSoup, url: str) -> None:
        soup.prettify()
        html_str = str(soup)
        output_path = self.get_path_from_url(url)
        with open(output_path, "wb") as f:
            f.write(html_str.encode("utf-8").strip())
        LOG.info(f"saved page to {output_path}")

    def get_filename_from_url(self, url: str) -> str:
        id = urllib.parse.urlparse(url).path[1:]
        filename = id[: id.rfind("-")].lower() + ".html"
        if url == self.url:
            filename = "index.html"
        return filename

    @trace()
    def get_path_from_url(self, url: str) -> Path:
        filename = self.get_filename_from_url(url)
        return Path(self.output_dir + "/" + filename)


class Scraper:
    def __init__(self, url: str, soup: BeautifulSoup, driver: webdriver.Chrome, file_manager: FileManager, timeout: float = 10, dark_mode: bool = False):
        self.url = url
        self.soup = soup
        self.driver = driver
        self.file_manager = file_manager
        self.timeout = timeout
        self.dark_mode = dark_mode
        self.will_visit = set([url])
        self.visited = set()

    def run(self) -> None:
        while self.will_visit:
            url = self.will_visit.pop()

            self._load_page(url)
            self._expand_toggle_blocks()
            soup = BeautifulSoup(self.driver.page_source, "html5lib")
            self._clean_up(soup)
            self._download_images(soup)
            self._download_stylesheets(soup)
            self._insert_injections(soup)
            subpage_urls = self._link_to_subpages(soup)
            self._link_to_table_view_subpages(soup)

            self.file_manager.save_page(soup, url)
            self.visited.add(url)
            self.will_visit.update(page for page in subpage_urls if page not in self.visited)

        self.driver.quit()
        LOG.info("exiting scraper")

    @trace()
    def _load_page(self, url: str) -> None:
        LOG.info("loading page")
        prev_page = ""

        def is_page_loaded(d: webdriver.Chrome) -> bool:
            nonlocal prev_page
            root_block = d.find_elements(By.CLASS_NAME, "notion-presence-container")
            if root_block:
                unknown_blocks = d.find_elements(By.CLASS_NAME, "notion-unknown-block")
                loading_spinners = d.find_elements(By.CLASS_NAME, "loading-spinner")
                scrollers = d.find_elements(By.CLASS_NAME, "notion-scroller")
                scrollers_with_children = [scroller for scroller in scrollers if scroller.find_elements(By.TAG_NAME, "div")]
                page_changed = prev_page != d.page_source
                all_scrollers_loaded = len(scrollers_with_children) == len(scrollers)
                if all_scrollers_loaded and not unknown_blocks and not loading_spinners and not page_changed:
                    return True
                LOG.info(f"waiting for: {len(unknown_blocks)} unknown blocks - {len(loading_spinners)} loading spinners - {len(scrollers_with_children)}/{len(scrollers)} scrollers with children")
            prev_page = d.page_source
            return False

        self.driver.get(url)
        # In tests there were always infinite spinners so we ignore the timeout
        # Maybe fix later
        try:
            WebDriverWait(self.driver, self.timeout).until(is_page_loaded)
        except Exception:
            LOG.info("Timed out waiting for page to load, proceeding anyways")
        LOG.info("page loaded")

        mode = "dark" if self.dark_mode else "light"
        self.driver.execute_script("__console.environment.ThemeStore.setState({ mode: '" + mode + "' })")
        LOG.info(f"set theme to {mode}")

    @trace(print_args=False)
    def _expand_toggle_blocks(self, expanded_toggle_blocks=[]) -> None:
        def get_toggle_blocks() -> List[WebElement]:
            toggle_blocks = self.driver.find_elements(By.CLASS_NAME, "notion-toggle-block")
            header_toggle_blocks = []
            queries = [f"notion-selectable.notion-{type}-block" for type in ["header", "sub_header", "sub_sub_header"]]
            for query in queries:
                blocks = self.driver.find_elements(By.CLASS_NAME, query)
                for block in blocks:
                    if block.find_elements(By.CSS_SELECTOR, "div[role=button]"):
                        header_toggle_blocks.append(block)
            toggle_blocks += header_toggle_blocks
            return toggle_blocks

        def is_block_expanded(b: WebElement) -> bool:
            content = b.find_element(By.CSS_SELECTOR, "div:not([style]")
            unknown_children = b.find_elements(By.CLASS_NAME, "notion-unknown-block")
            is_loading = b.find_elements(By.CLASS_NAME, "loading-spinner")
            return content and not unknown_children and not is_loading

        toggle_blocks = get_toggle_blocks()
        toggle_blocks = [block for block in toggle_blocks if block not in expanded_toggle_blocks]

        for block in toggle_blocks:
            toggle_block_button = block.find_element(By.CSS_SELECTOR, "div[role=button]")
            is_expanded = "(180deg)" in (toggle_block_button.find_element(By.TAG_NAME, "svg").get_attribute("style"))
            if not is_expanded:
                self.driver.execute_script("arguments[0].click();", toggle_block_button)
                try:
                    WebDriverWait(self.driver, self.timeout).until(lambda d: is_block_expanded(block))
                except TimeoutException:
                    LOG.warning("timeout while expanding block - manually check if it's expanded in the snapshot", file=sys.stderr)
                    continue
            expanded_toggle_blocks.append(block)

        nested_toggle_blocks = [block for block in get_toggle_blocks() if block not in expanded_toggle_blocks]
        LOG.info(f"expanded {len(expanded_toggle_blocks)} toggle blocks so far - found {len(nested_toggle_blocks)} to expand next")
        if nested_toggle_blocks:
            self._expand_toggle_blocks(expanded_toggle_blocks)

    @trace()
    def _clean_up(self, soup: BeautifulSoup) -> None:
        for script in soup.findAll("script"):
            script.decompose()
        for aif_production in soup.findAll("iframe", {"src": "https://aif.notion.so/aif-production.soup"}):
            aif_production.decompose()
        for intercom_frame in soup.findAll("iframe", {"id": "intercom-frame"}):
            intercom_frame.decompose()
        for intercom_div in soup.findAll("div", {"class": "intercom-lightweight-app"}):
            intercom_div.decompose()
        for overlay_div in soup.findAll("div", {"class": "notion-overlay-container"}):
            overlay_div.decompose()
        for vendors_css in soup.find_all("link", href=lambda text: bool(text) and "vendors~" in text):
            vendors_css.decompose()
        for collection_selector in soup.findAll("div", {"class": "notion-collection-view-select"}):
            collection_selector.decompose()
        for tag in ["description", "twitter:card", "twitter:site", "twitter:title", "twitter:description", "twitter:image", "twitter:url", "apple-itunes-app"]:
            unwanted_tag = soup.find("meta", attrs={"name": tag})
            if unwanted_tag and isinstance(unwanted_tag, Tag):
                unwanted_tag.decompose()
        for tag in ["og:site_name", "og:type", "og:url", "og:title", "og:description", "og:image"]:
            unwanted_og_tag = soup.find("meta", attrs={"property": tag})
            if unwanted_og_tag and isinstance(unwanted_og_tag, Tag):
                unwanted_og_tag.decompose()
        LOG.info("removed unwanted tags from html")

    @trace()
    def _download_images(self, soup: BeautifulSoup) -> None:
        images = [img for img in soup.findAll("img") if img.has_attr("src") and not (img.has_attr("class") and "notion-emoji" in img["class"])]
        LOG.info(f"found {len(images)} images to download")
        for img in images:
            is_notion_asset = img["src"].startswith("/")
            if "data:image" not in img["src"]:
                img_src = img["src"]
                if is_notion_asset:
                    img_src = f'https://www.notion.so{img["src"]}'
                img["src"] = self.file_manager.download_asset(img_src)
            elif is_notion_asset:
                img["src"] = f'https://www.notion.so{img["src"]}'

        emojis = [img for img in soup.findAll("img") if img.has_attr("class") and "notion-emoji" in img["class"]]
        LOG.info(f"found {len(emojis)} emojis to download")
        for img in emojis:
            style = cssutils.parseStyle(img["style"])
            spritesheet = style["background"]
            spritesheet_url = spritesheet[spritesheet.find("(") + 1 : spritesheet.find(")")]
            download_path = self.file_manager.download_asset(f"https://www.notion.so{spritesheet_url}")
            style["background"] = spritesheet.replace(spritesheet_url, download_path)
            img["style"] = style.cssText

        is_in_both = lambda img: img in images and img in emojis
        shared = [is_in_both(img) for img in images + emojis]
        assert not any(shared), "img is both an image and an emoji"

    @trace()
    def _download_stylesheets(self, soup: BeautifulSoup) -> None:
        is_stylesheet = lambda link: link.has_attr("href") and link["href"].startswith("/") and not "vendors~" in link["href"]
        stylesheets = [link for link in soup.findAll("link", rel="stylesheet") if is_stylesheet(link)]

        for link in stylesheets:
            LOG.debug("link:" + link["href"])
            download_path = self.file_manager.download_asset(f'https://www.notion.so{link["href"]}')

            with open(os.path.join(self.file_manager.output_dir, download_path), "rb+") as f:
                stylesheet = cssutils.parseString(f.read())

                # additionally download fonts used in the stylesheet
                for rule in stylesheet.cssRules:
                    if rule.type == cssutils.css.CSSRule.FONT_FACE_RULE:
                        # for some reason the url becomes https:/www.notion.so/https:/not.../<the url>
                        url_regex = re.compile(r"url\((/?https:/www.notion.so)*(.+?)\).*")
                        LOG.debug(rule.style["src"])
                        m = url_regex.match(rule.style["src"])
                        if m is None:
                            raise ValueError("Could not parse stylesheet source of font face rule")
                        # first capture group the repeating notion url
                        font_file = re.sub(r"assets/", "", m.group(2))
                        parent_css_path = os.path.split(urllib.parse.urlparse(link["href"]).path)[0]
                        LOG.debug("parent_css_path: " + parent_css_path)
                        LOG.debug("font_file: " + font_file)
                        font_download_url = "/".join(p.strip("/") for p in ["https://www.notion.so", parent_css_path, font_file] if p.strip("/"))
                        LOG.debug("font_download_url: " + font_download_url)
                        font_download_path = self.file_manager.download_asset(font_download_url, Path(font_file).name)
                        rule.style["src"] = f"url({font_download_path})"
                f.seek(0)
                f.truncate()
                f.write(stylesheet.cssText)

            link["href"] = download_path

    @trace()
    def _insert_injections(self, soup: BeautifulSoup) -> None:
        # add ids and classes for 'injection.js' to work
        toggle_blocks = soup.findAll("div", {"class": "notion-toggle-block"})
        for query in ["header", "sub_header", "sub_sub_header"]:
            header_toggle_blocks = soup.findAll("div", {"class": f"notion-selectable notion-{query}-block"})
            [toggle_blocks.append(block) for block in header_toggle_blocks if block.select_one("div[role=button]") is not None]
        for toggle_block in toggle_blocks:
            toggle_id = uuid.uuid4()
            toggle_button = toggle_block.select_one("div[role=button]")
            toggle_content = toggle_block.find("div", {"class": None, "style": ""})
            if toggle_button and toggle_content:
                toggle_button["class"] = toggle_block.get("class", []) + ["notionsnapshot-toggle-button"]
                toggle_content["class"] = toggle_content.get("class", []) + ["notionsnapshot-toggle-content"]
                toggle_content.attrs["notionsnapshot-toggle-id"] = toggle_button.attrs["notionsnapshot-toggle-id"] = toggle_id

        css_path, js_path = self.file_manager.copy_injections_to_assets()
        assert soup.head is not None
        soup.head.insert(-1, soup.new_tag("link", rel="stylesheet", href=css_path))
        assert soup.body is not None
        soup.body.insert(-1, soup.new_tag("script", type="text/javascript", src=js_path))

    @trace()
    def _link_to_table_view_subpages(self, soup: BeautifulSoup) -> None:
        # TODO: refactor -> this is actually a part of _link_to_subpages()
        # test with: https://eager-waterfall-308.notion.site/2604ce45890645c79f67d92833083fee?v=e138f6fdcea24f87b442577732b2052d

        tables = soup.findAll("div", {"class": "notion-table-view"})
        LOG.info(f"found {len(tables)} tables")
        for table in tables:
            rows = table.findAll("div", {"class": "notion-collection-item"})
            LOG.info(f"found {len(rows)} rows with links to subpages in table")
            for row in rows:
                subpage_path = "/" + row["data-block-id"].replace("-", "")
                row_name_span = row.find("span")
                row_name_span["style"] = row_name_span["style"].replace("pointer-events: none;", "")
                subpage_anchor = soup.new_tag(
                    "a",
                    attrs={"href": subpage_path, "style": "cursor: pointer; color: inherit; text-decoration: none; fill: inherit;"},
                )
                row_name_span.wrap(subpage_anchor)

    @trace()
    def _link_to_subpages(self, soup: BeautifulSoup) -> List[str]:
        # THIS DOESN'T WORK YET

        subpage_urls = []
        domain = f'{self.url.split("notion.site")[0]}notion.site'
        anchors = soup.find_all("a", href=True)
        for a in anchors:
            url = a["href"]

            is_relative_url = url.startswith("/")
            if is_relative_url:
                url = f'{domain}/{a["href"].split("/")[-1]}'

            is_external_url = not url.startswith(domain)
            if is_external_url:
                continue
            scroller_parent = a.find_parent("div", class_="notion-scroller")
            is_scroller = scroller_parent is not None and len(scroller_parent) > 0
            is_table_of_contents = "#" in url

            if is_table_of_contents:
                # add ids and classes for 'injection.js' to work
                arr = url.split("#")
                url = arr[0]
                a["href"] = f"#{arr[-1]}"
                a["class"] = a.get("class", []) + ["notionsnapshot-anchor-link"]

            elif is_scroller:
                filename = self.file_manager.get_filename_from_url(url)
                LOG.info("file: " + filename)
                a["href"] = filename
                subpage_urls.append(url)
            else:
                del a["href"]
                a.name = "span"
                children = [child for child in ([a] + a.find_all()) if child.has_attr("style")]
                for child in children:
                    style = cssutils.parseStyle(child["style"])
                    style["cursor"] = "default"
                    child["style"] = style.cssText

        return subpage_urls


if __name__ == "__main__":
    args = ArgParser.get_arguments()
    file_manager = FileManager(args.url, cache_assets=(not args.no_cache))
    driver = DriverInitializer.get_driver(args)
    soup = BeautifulSoup(driver.page_source, "html5lib")

    if args.debug:
        LOG.setLevel(logging.DEBUG)

    Scraper(args.url, soup, driver, file_manager, timeout=args.timeout, dark_mode=args.dark_mode).run()
