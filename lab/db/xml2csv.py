# Converts one file of a Stack Exchange data dump (one <row attr="..."/> per record) from XML on stdin to CSV on stdout,
# for COPY: python3 xml2csv.py Posts < Posts.xml
# Faithful: one column per attribute, in the order below; an absent attribute is NULL (unquoted empty), any present value
# is quoted, so an empty string stays an empty string. Every cleanup happens in stackexchange.sql.
import sys
import xml.etree.ElementTree as ET

COLUMNS = {
    "Users": ["Id", "Reputation", "CreationDate", "DisplayName", "LastAccessDate", "WebsiteUrl", "Location", "AboutMe",
              "Views", "UpVotes", "DownVotes", "AccountId"],
    "Posts": ["Id", "PostTypeId", "AcceptedAnswerId", "ParentId", "CreationDate", "Score", "ViewCount", "Body",
              "OwnerUserId", "OwnerDisplayName", "LastEditorUserId", "LastEditorDisplayName", "LastEditDate",
              "LastActivityDate", "Title", "Tags", "AnswerCount", "CommentCount", "FavoriteCount", "ClosedDate",
              "CommunityOwnedDate", "ContentLicense"],
    "Tags": ["Id", "TagName", "Count", "ExcerptPostId", "WikiPostId"],
    "Comments": ["Id", "PostId", "Score", "Text", "CreationDate", "UserDisplayName", "UserId"],
    "Votes": ["Id", "PostId", "VoteTypeId", "UserId", "BountyAmount", "CreationDate"],
    "Badges": ["Id", "UserId", "Name", "Date", "Class", "TagBased"],
    "PostHistory": ["Id", "PostHistoryTypeId", "PostId", "RevisionGUID", "CreationDate", "UserId", "UserDisplayName",
                    "Comment", "Text", "ContentLicense"],
    "PostLinks": ["Id", "CreationDate", "PostId", "RelatedPostId", "LinkTypeId"],
}


def field(value):
    return "" if value is None else '"' + value.replace('"', '""') + '"'


name = sys.argv[1]
columns = COLUMNS[name]
known = set(columns)
out = sys.stdout
out.write(",".join(columns) + "\n")
for _, element in ET.iterparse(sys.stdin.buffer):
    if element.tag != "row":
        continue
    unknown = set(element.attrib) - known
    if unknown:  # a dump with more columns than we know: fail rather than drop data silently
        sys.exit(f"{name}.xml: unexpected attributes {sorted(unknown)}")
    out.write(",".join(field(element.get(column)) for column in columns) + "\n")
    element.clear()
