import logging
import logging.config
import os
import argparse
from urllib.parse import urlparse, urlunparse

from attr import dataclass
import requests
import bs4
from markdownify import MarkdownConverter
from atlassian import Confluence


ATTACHMENT_FOLDER_NAME = "attachments"
DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024   # 4MB, since we're single threaded this is safe to raise much higher


class ExportException(Exception):
    pass

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
    def __init__(self, url, token, out_dir, space):
        self.__out_dir = out_dir
        self.parsed_url = urlparse(url)
        self.token = token
        self.__seen = set()

        self.confluence = Confluence(url=urlunparse(self.parsed_url), token=self.token)
        self.space = space

    def _sanitize_filename(self, document_name_raw):
        document_name = document_name_raw
        for invalid in ["..", "/", ">", "<", ":", "\"", "|", "?", "*", "\\"]:
            if invalid in document_name:
                logging.warning("Dangerous page title: %s, %s found, replacing it with \"_\"", document_name, invalid)
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
        extension = ".html"
        if len(child_ids) > 0:
            document_name = "index"
        else:
            document_name = page_title

        # make some rudimentary checks, to prevent trivial errors
        sanitized_filename = self._sanitize_filename(document_name) + extension
        sanitized_parents = list(map(self._sanitize_filename, parents))

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

        self.page_action(page_meta_data)
    
        # recurse to process child nodes
        for child_id in page_meta_data.child_ids:
            self._handle_page(child_id, parents=page_meta_data.sanitized_parents + [page_meta_data.page_title])

    def _handle_space(self, space):
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
            self._handle_page(homepage_id, parents=[space_key])

    
    def handle_instance(self):
        start = 900 # todo: revert
        limit = 50
        
        while True:
            logging.debug ("start %s", start)
            ret = self.confluence.get_all_spaces(start=start, limit=limit, expand='description.plain,homepage')
            if ret['size'] <= 0:
                break
            for space in ret["results"]:
                if self.space is not None and space["key"] == self.space:
                    self._handle_space(space)
                    return
                if self.space is None:
                    self._handle_space(space)
            start += limit

class Exporter(ConfluenceWorker):
    def __init__(self, url, token, out_dir, space, no_attach):
        super().__init__(url, token, out_dir, space)
        self.__no_attach = no_attach


    def __handle_attachment(self, att_title, download, page_meta_data: PageMetadata):
        ret = self.confluence.get_attachments_from_content(page_meta_data.page_id, start=0, limit=500, expand=None,
                                                                filename=None, media_type=None)
        for i in ret["results"]:
            att_title = i["title"]
            download = i["_links"]["download"]

            prefix = self.parsed_url.path
            att_url = urlunparse(
                (self.parsed_url[0], self.parsed_url[1], prefix + download.lstrip("/"), None, None, None)
            )
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

        os.makedirs(page_meta_data.page_output_dir, exist_ok=True)
        logging.debug("Saving to %s", "/".join(page_meta_data.page_location))
        with open(page_meta_data.page_filename, "w", encoding="utf-8") as f:
            f.write(page_meta_data.content)

        # fetch attachments unless disabled
        if not self.__no_attach:
            self.__handle_attachment(None, None, page_meta_data)



class Converter:
    def __init__(self, out_dir):
        self.__out_dir = out_dir

    def recurse_findfiles(self, path):
        for entry in os.scandir(path):
            if entry.is_dir(follow_symlinks=False):
                yield from self.recurse_findfiles(entry.path)
            elif entry.is_file(follow_symlinks=False):
                yield entry
            else:
                raise NotImplementedError

    def __convert_atlassian_html(self, soup):
        for image in soup.find_all("ac:image"):
            url = None
            for child in image.children:
                url = child.get("ri:filename", None)
                break

            if url is None:
                # no url found for ac:image
                continue

            # construct new, actually valid HTML tag
            srcurl = os.path.join(ATTACHMENT_FOLDER_NAME, url)
            imgtag = soup.new_tag("img", attrs={"src": srcurl, "alt": srcurl})

            # insert a linebreak after the original "ac:image" tag, then replace with an actual img tag
            image.insert_after(soup.new_tag("br"))
            image.replace_with(imgtag)
        return soup

    def convert(self):
        for entry in self.recurse_findfiles(self.__out_dir):
            path = entry.path

            if not path.endswith(".html"):
                continue

            logging.info("Converting %s", path)
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()

            soup_raw = bs4.BeautifulSoup(data, 'html.parser')
            soup = self.__convert_atlassian_html(soup_raw)

            md = MarkdownConverter().convert_soup(soup)
            newname = os.path.splitext(path)[0]
            with open(newname + ".md", "w", encoding="utf-8") as f:
                f.write(md)


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
        parser.add_argument("--mark-migrated", action="store_true", dest="mark_migrated", required=False, 
                            help="Mark pages as migrated when corresponding markdown file in out_dir exists")
        args = parser.parse_args()
        

        if args.mark_migrated:
            # dumper = Marker(url=args.url, token=args.token, out_dir=args.out_dir,
            #                 space=args.space, mark_migrated=True)
            return

        if not args.no_fetch:
            exporter = Exporter(url=args.url, token=args.token, out_dir=args.out_dir,
                            space=args.space, no_attach=args.no_attach)
            exporter.handle_instance()
            
        converter = Converter(out_dir=args.out_dir)
        converter.convert()

    init_logging()
    main()