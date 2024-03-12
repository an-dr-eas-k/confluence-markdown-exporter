### A very simple Confluence to Markdown exporter extended by the functionality to flag exported pages on confluence

Not to forget, it started as fork of https://github.com/gergelykalman/confluence-markdown-exporter

I extended the original exporter by the functionality to flag the set of exported pages on the original confluence page. This allows me to use the tools for migrating to another markdown based destination.

My usecase is the following:
1. Export all pages from a Confluence Space into a given local directory (the original function of the script)
1. Delete all html files
1. Manually delete all markdown files not supposed to be migrated to the new destination
1. Run the Flag function

   a) It will process all confluence pages, that are present locally in markdown format. These are considered as selected for migration.

   b) It will update the local content of the markdown file in case a change has happend on the source Confluence Space.

   c) It will update the remote Confluence page on the source Space
   
      - and apply the given template html file to encompass the original content by html which flags the original content as migrated. The comment ```<!-- migrated content -->``` will be replaced by the original content.
      - and modify the page title with the suffix " (migrated)"

This code is not written with security in mind, do NOT run it on a repository that can contain mailicious
page titles.


### Usage

1. Install requirements: ```pip3 install -r requirements.txt```
1. Obtain a Confluence API token under Profile -> Security -> Manage API Tokens

#### Run Export Function

3. Run the script: ```python3.9 confluence-markdown-export.py <url> <token> <out_dir>```

   providing 
   * URL e.g. https://YOUR_PROJECT.atlassian.net, 
   * login details - API Token, and
   * output directory, e.g. ./output_dir

#### Flag exported pages on confluence


4. Run the script: ```python3.9 confluence-markdown-export.py --space <space> --flag-migrated flagging.template.html <url> <token> <local_dir>``` similar to the export function. 

   this time, 
   * the &lt;local_dir&gt; directory contains the set of migrated confluence pages that should be flagged in the source.
   * you need the ```--flag-migrated <filename>``` argument to select your template file.


