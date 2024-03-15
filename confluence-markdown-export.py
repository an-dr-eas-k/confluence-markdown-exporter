import logging
import logging.config
import os
import re
import argparse
from urllib.parse import urlparse, urlunparse

from attr import dataclass
import requests
import bs4
from markdownify import MarkdownConverter
from atlassian import Confluence


ATTACHMENT_FOLDER_NAME = "attachments"
INDEX_FILE_NAME = "readme"
SKIP_TAGS = "custom:skip"
SKIPPED_PLACEHOLDER = "CUSTOMSKIPPEDCONTENT"
DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024   # 4MB, since we're single threaded this is safe to raise much higher


class ExportException(Exception):
    pass

class Converter:
    def __init__(self, out_dir):
        self.__out_dir = out_dir
        self.__target_files = self.get_file_base(self.__out_dir)

    def recurse_findfiles(self, path):
        for entry in os.scandir(path):
            if entry.is_dir(follow_symlinks=False):
                yield from self.recurse_findfiles(entry.path)
            elif entry.is_file(follow_symlinks=False):
                yield entry
            else:
                raise NotImplementedError

    def _convert_atlassian_html(self, soup: bs4.BeautifulSoup, file_path) -> bs4.BeautifulSoup:
        soup = Converter._convert_atlassian_image(soup)
        soup = Converter._convert_atlassian_code(soup)
        if file_path is not None and self.__target_files is not None:
            soup = Converter._convert_atlassian_link(soup, file_path, self.__target_files)
        return soup

    def _convert_atlassian_link(soup: bs4.BeautifulSoup, file_path: str, file_base):
        def tag_repl(soup: bs4.BeautifulSoup, item_content) -> bs4.Tag:
            link_name = None
            link_url = None
            if item_content.name == "ri:page":
                referenced_document = item_content.get("ri:content-title")
                link_name = True \
                    and item_content.parent.find("ac:plain-text-link-body") and item_content.parent.find("ac:plain-text-link-body").text \
                    or item_content.get("ri:content-title")
                pattern = f"\\\\{re.escape(referenced_document)}([/\\\\]{INDEX_FILE_NAME})?\\.(md|html)$"
                matches = [x for x in file_base if re.search(pattern, x)]
                if "Run Client tech" in file_path:
                    print()
                if len(matches) > 2:
                    logging.warning("%s: Ambiguous link name: %s, found multiple matches: %s", file_path, link_name, matches)
                link_url = matches[0] if len(matches) > 0 else None
                link_url = link_url and os.path.relpath(link_url, os.path.dirname(file_path)) \
                    .replace(" ", "%20") \
                    .replace("\\", "/") \
                    .replace(".html", ".md")
            elif item_content.name == "ri:attachment":
                link_name = None \
                    and item_content.find("ac:plain-text-body") and item_content.find("ac:plain-text-body").text \
                    or item_content.get("ri:filename")
                link_url = os.path.join(ATTACHMENT_FOLDER_NAME, item_content.get("ri:filename")).replace(" ", "%20")

            if link_name is None:
                return None

            link_tag = soup.new_tag("a", attrs={"href": link_url})
            link_tag.string = link_name
            return link_tag

        return Converter._convert_atlassian( \
            soup=soup, \
            list_finder=lambda soup: soup.find_all("ac:link"), \
            item_finder=lambda list_item: list_item.find("ri:page") or list_item.find("ri:attachment"), \
            tag_replacement=tag_repl
        )

    def _convert_atlassian_image(soup):
        def tag_repl(soup: bs4.BeautifulSoup, item_content) -> bs4.Tag:
            srcurl = None
            if item_content.name == "ri:attachment":
                srcurl = os.path.join(ATTACHMENT_FOLDER_NAME, item_content.get("ri:filename")).replace(" ", "%20")
            if item_content.name == "ri:url":
                srcurl = item_content.get("ri:value")
            if srcurl is None:
                return None
            outer = soup.new_tag("div")
            outer.append(soup.new_tag("img", attrs={"src": srcurl, "alt": srcurl}))
            outer.append(soup.new_tag("br"))
            return outer
            

        return Converter._convert_atlassian( \
            soup=soup, \
            list_finder=lambda soup: soup.find_all("ac:image"), \
            item_finder=lambda list_item: list_item.find("ri:attachment", None) or list_item.find("ri:url", None), \
            tag_replacement=tag_repl
        )

    def _convert_atlassian_code(soup: bs4.BeautifulSoup) -> bs4.BeautifulSoup:
        def tag_repl(soup: bs4.BeautifulSoup, item_content) -> bs4.Tag:
            tag = soup.new_tag("pre")
            tag.string = item_content
            return tag

        return Converter._convert_atlassian( \
            soup=soup, \
            list_finder=lambda soup: soup.find_all("ac:structured-macro", attrs={"ac:name": "code"}), \
            item_finder=lambda list_item: list_item.find("ac:plain-text-body").text, \
            tag_replacement=tag_repl
        )

    def _preprocess_skipped_tags(self, soup: bs4.BeautifulSoup, skipped_items: list) -> bs4.BeautifulSoup:

        def tag_repl(soup: bs4.BeautifulSoup, item_content: bs4.Tag) -> bs4.Tag:
            tag = soup.new_tag("div", attrs={"class": "skipped"})
            tag.string = SKIPPED_PLACEHOLDER
            
            if item_content.name == "span":
                item_content.attrs["style"] = "color: #004a77"

            skipped_items.append(item_content)
            return tag

        def items_to_skip(tag: bs4.Tag):
            return False \
                or tag.name in ['ul', 'ol'] and tag.find_parent('table') is not None \
                or tag.name == 'span' and tag.attrs.get("style") and "color" in tag.attrs.get("style")

        return Converter._convert_atlassian( \
            soup=soup, \
            list_finder=lambda soup: soup.find_all(items_to_skip),
            item_finder=lambda list_item: list_item, \
            tag_replacement=tag_repl
        )

    def _convert_atlassian(soup: bs4.BeautifulSoup, list_finder, item_finder, tag_replacement) -> bs4.BeautifulSoup:
        for list_item in list_finder(soup):
            item_content = item_finder(list_item)

            if item_content is None:
                continue

            replacement_tag = tag_replacement(soup, item_content)
            if replacement_tag is None:
                continue

            list_item.replace_with(replacement_tag)
        return soup

    def _postprocess_skipped_tags(self, md: str, skipped_items: list[bs4.Tag]) -> str:
        for skipped_item in skipped_items:
            md = md.replace(SKIPPED_PLACEHOLDER, skipped_item.prettify().replace("\n", ""), 1)
        return md

    def convert_file_content(self, content: str, file_path = None) -> str:
        soup = bs4.BeautifulSoup(content, 'html.parser')

        skipped_list = list()
        soup: bs4.BeautifulSoup = self._preprocess_skipped_tags(soup, skipped_list)

        soup: bs4.BeautifulSoup = self._convert_atlassian_html(soup, file_path)

        self._save_adjusted_html(soup, file_path)

        md: str = MarkdownConverter(keep_inline_images_in=["td", "table", "tr", "p", "div", "tbody"]).convert_soup(soup)
        md = self._postprocess_skipped_tags(md, skipped_list)
        return md

    def _save_adjusted_html(self, soup: bs4.BeautifulSoup, file_path):
        try:
            adjusted_file = file_path + ".adjusted.htm"
            with open(adjusted_file, "w", encoding="utf-8") as f:
                f.write(soup.prettify())
        except Exception as _:
            logging.debug("Could not write adjusted HTML to file %s, skipping", adjusted_file)
            pass

    def get_file_base(self, path):
        target_files = set()
        for entry in self.recurse_findfiles(self.__out_dir):
            path = entry.path

            if not path.endswith(".html") and not path.endswith(".md"):
                continue

            target_files.add(path)

        return target_files

    def convert(self):

        for path in [x for x in self.__target_files if x.endswith(".html")]:
            self.convert_file(path)

    def convert_file(self, path):
        logging.debug("Converting %s", path)
        
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()

        md = self.convert_file_content(data, path)
        newname = os.path.splitext(path)[0]
        with open(newname + ".md", "w", encoding="utf-8") as f:
            f.write(md)


@dataclass
class PageMetadata:
    page_title: str
    page_id: str
    child_ids: list
    content: str
    document_name: str
    sanitized_filename: str
    sanitized_parents: str
    page_location: str
    page_filename: str
    page_output_dir: str

class ConfluenceWorker:

    file_extension = ".html"

    def __init__(self, url, token, out_dir, space, ignore_titles: [] = None):
        self.__out_dir = out_dir
        self.parsed_url = urlparse(url)
        self.token = token
        self.space = space
        self.ignore_titles = ignore_titles
        self.__seen = set()

        self.confluence = Confluence(url=urlunparse(self.parsed_url), token=self.token)

    def get_page_url(self, suffix = ""):
        prefix = self.parsed_url.path
        return urlunparse(
            (self.parsed_url[0], self.parsed_url[1], prefix + suffix.lstrip("/"), None, None, None)
        )

    def _sanitize_filename(self, document_name_raw):
        document_name = document_name_raw
        for invalid in ["..", "/", ">", "<", ":", "\"", "|", "?", "*", "\\"]:
            if invalid in document_name:
                logging.debug("Dangerous page title: %s, %s found, replacing it with \"_\"", document_name, invalid)
                document_name = document_name.replace(invalid, "_")
        return document_name

    def _get_page(self, src_id):
        if src_id in self.__seen:
            # this could theoretically happen if Page IDs are not unique or there is a circle
            raise ExportException("Duplicate Page ID Found!")

        return self.confluence.get_page_by_id(src_id, expand="body.storage")

    def _obtain_page_metadata(self, page, parents) -> PageMetadata:
        page_title = page["title"]
        page_id = page["id"]
    
        # see if there are any children
        child_ids = []
        try:
            child_ids = self.confluence.get_child_id_list(page_id)
        except Exception as _:
            logging.error ("Error getting child ids for page %s", page_id)
    
        content = page["body"]["storage"]["value"]

        # save all files as .html for now, we will convert them later
        document_name = page_title
        sanitized_parents = list(map(self._sanitize_filename, parents))

        if len(child_ids) > 0:
            document_name = INDEX_FILE_NAME
            sanitized_parents = list(map(self._sanitize_filename, parents+[page_title]))

        # make some rudimentary checks, to prevent trivial errors
        sanitized_filename = self._sanitize_filename(document_name) + self.file_extension

        page_location = sanitized_parents + [sanitized_filename]
        page_filename = os.path.join(self.__out_dir, *page_location)

        page_output_dir = os.path.dirname(page_filename)

        self.__seen.add(page_id)

        return PageMetadata(page_title, page_id, child_ids, content, document_name, sanitized_filename, 
                            sanitized_parents, page_location, page_filename, page_output_dir)

    def page_action(self, page_meta_data: PageMetadata):
        pass

    def _handle_page(self, src_id, parents):
        page = self._get_page(src_id)
        page_meta_data: PageMetadata = self._obtain_page_metadata(page, parents)

        if self.ignore_titles and any(re.match(f"{ignored_title.lower()}", page_meta_data.page_title.lower()) for ignored_title in self.ignore_titles):
            logging.info("Ignoring page: %s", page_meta_data.page_title)
            return
        self.page_action(page_meta_data)
    
        # recurse to process child nodes
        for child_id in page_meta_data.child_ids:
            self._handle_page(child_id, parents=page_meta_data.sanitized_parents)

    def _handle_space(self, space, ignore_space: bool = False):
        space_key = space["key"]
        logging.info("Processing space %s", space_key)
        if space.get("homepage") is None:
            logging.error(
                "Skipping space: %s, no homepage found!\nIn order for this tool to work there has to be a root page!", 
                space_key)
            raise ExportException("No homepage found")
        else:
            # homepage found, recurse from there
            homepage_id = space["homepage"]["id"]
            self._handle_page(homepage_id, parents=([] if ignore_space else [space_key]))

    
    def handle_instance(self):
        start = 900 # todo: revert
        limit = 50
        
        while True:
            logging.debug ("start %s", start)
            ret = self.confluence.get_all_spaces(start=start, limit=limit, expand='description.plain,homepage')
            try:
                if ret['size'] <= 0:
                    break
            except Exception as e:
                logging.error("Error getting spaces: %s", e)
                break
            for space in ret["results"]:
                if self.space is not None and space["key"] == self.space:
                    self._handle_space(space, True)
                    return
                if self.space is None:
                    self._handle_space(space)
            start += limit

class Exporter(ConfluenceWorker):
    def __init__(self, url, token, out_dir, space, ignore_titles, no_attach):
        super().__init__(url, token, out_dir, space, ignore_titles)
        self.__no_attach = no_attach


    def __handle_attachment(self, page_meta_data: PageMetadata):
        ret = self.confluence.get_attachments_from_content(page_meta_data.page_id, start=0, limit=500, expand=None,
                                                                filename=None, media_type=None)
        for i in ret["results"]:
            att_title = i["title"]
            download = i["_links"]["download"]

            att_url = self.get_page_url(download)

            att_sanitized_name = self._sanitize_filename(att_title)
            att_filename = os.path.join(page_meta_data.page_output_dir, ATTACHMENT_FOLDER_NAME, att_sanitized_name)

            att_dirname = os.path.dirname(att_filename)
            os.makedirs(att_dirname, exist_ok=True)

            logging.debug("Saving attachment %s to %s", att_title, page_meta_data.page_location)

            r = requests.get(att_url, headers={"Authorization": f"Bearer {self.token}"}, stream=True)
            if 400 <= r.status_code:
                if r.status_code == 404:
                    logging.warning("Attachment %s not found (404)!", att_url)
                    continue

                # this is a real error, raise it
                r.raise_for_status()

            with open(att_filename, "wb") as f:
                for buf in r.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    f.write(buf)

    def page_action(self, page_meta_data: PageMetadata):
        super().page_action(page_meta_data)

        if len(page_meta_data.content.strip()) <= 1:
            return

        os.makedirs(page_meta_data.page_output_dir, exist_ok=True)
        logging.debug("Saving to %s", "/".join(page_meta_data.page_location))
        with open(page_meta_data.page_filename, "w", encoding="utf-8") as f:
            f.write(page_meta_data.content)

        # fetch attachments unless disabled
        if not self.__no_attach:
            self.__handle_attachment(page_meta_data)

class Marker(ConfluenceWorker):

    file_extension = ".md"

    def __init__(self, url, token, out_dir, space, flag_migrated_template):
        super().__init__(url, token, out_dir, space)
        with open(flag_migrated_template, "r", encoding="utf-8") as f:
            self.__flagging_template_content = f.read()
        self.__converter = Converter(out_dir)

    def flag_page_migrated(self, page_meta_data: PageMetadata):

        new_page_content = self.__flagging_template_content.replace(
            "<!-- migrated content -->", 
            page_meta_data.content)
        logging.info("new page content:\n%s", new_page_content)
        self.confluence.update_page(
            page_id=page_meta_data.page_id, 
            title="%s (migrated)" % page_meta_data.page_title, 
            body=new_page_content)

    def update_page(self, page_meta_data: PageMetadata):
        updated_content_md = self.__converter.convert_file_content(page_meta_data.content, page_meta_data.page_location)
        with open(page_meta_data.page_filename, "w", encoding="utf-8") as f:
            f.write(updated_content_md)

    def page_action(self, page_meta_data: PageMetadata):
        super().page_action(page_meta_data)

        if os.path.exists(page_meta_data.page_filename):
            logging.info("Updating %s and flagging as migrated", page_meta_data.page_location)
            self.update_page(page_meta_data)
            self.flag_page_migrated(page_meta_data)
            

if __name__ == "__main__":
    def init_logging():
        logging.config.fileConfig(os.path.dirname(os.path.abspath(__file__))+'/logging.ini')

    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("url", type=str, help="The url to the confluence instance")
        parser.add_argument("token", type=str, help="The access token to Confluence")
        parser.add_argument("out_dir", type=str, help="The directory to output the files to")
        parser.add_argument("--space", type=str, required=False, default=None, help="Spaces to export")
        parser.add_argument("--skip-attachments", action="store_true", dest="no_attach", required=False,
                            default=False, help="Skip fetching attachments")
        parser.add_argument("--no-fetch", action="store_true", dest="no_fetch", required=False,
                            default=False, help="This option only runs the markdown conversion")
        parser.add_argument("--flag-migrated", type=str, default=None, dest="flag_migrated", required=False, 
                            help="Flag pages as migrated when corresponding markdown file exists in out_dir")
        parser.add_argument("--ignore-titles", type=str, default=None, dest="ignore_titles", required=False, 
                            help="list a set of re patterns, comma separated, to ignore pages with these titles including their children")
        args = parser.parse_args()
        
        args.ignore_titles = [title.strip().lower() for title in (args.ignore_titles.split(",") if args.ignore_titles else [])]

        if args.flag_migrated:
            marker = Marker(url=args.url, token=args.token, out_dir=args.out_dir,
                            space=args.space, flag_migrated_template=args.flag_migrated)
            marker.handle_instance()
            return

        if not args.no_fetch:
            exporter = Exporter(url=args.url, token=args.token, out_dir=args.out_dir,
                            space=args.space, ignore_titles=args.ignore_titles, no_attach=args.no_attach)
            exporter.handle_instance()
            
        converter = Converter(out_dir=args.out_dir)
        converter.convert()

    init_logging()
    main()