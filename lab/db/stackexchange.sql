-- dba.stackexchange.com (Database Administrators Stack Exchange), from Stack Exchange's own data dump of 2024-04-06.
-- Runs once, when the image is built, in a single transaction (psql -1) in the stackexchange database. Each XML file of
-- the dump streams out of the archive through xml2csv.py into COPY, so none of it lands on disk.
--
-- Complete: every row and every value of the dump. The only changes are:
--   * snake_case names and proper types (all timestamps are UTC, as in the dump);
--   * post_tags, split out of posts.tags, and the post_types, vote_types and post_history_types lookups, whose names
--     come from Stack Exchange's schema documentation (meta.stackexchange.com/q/2677).
-- The dump leaves out deleted posts, but keeps some rows that point at them (111,161 votes, 22 history rows, 290 links,
-- 1 accepted answer) and 1 post owned by a deleted user. Those foreign keys are declared NOT VALID: Metabase still sees
-- them, and joins drop those rows.

\set ON_ERROR_STOP on

-- ---- Staging: the dump's files as text ---------------------------------------------------------------------------------

CREATE SCHEMA staging;

CREATE UNLOGGED TABLE staging.users (id text, reputation text, creation_date text, display_name text, last_access_date text,
  website_url text, location text, about_me text, views text, up_votes text, down_votes text, account_id text);
CREATE UNLOGGED TABLE staging.posts (id text, post_type_id text, accepted_answer_id text, parent_id text, creation_date text,
  score text, view_count text, body text, owner_user_id text, owner_display_name text, last_editor_user_id text,
  last_editor_display_name text, last_edit_date text, last_activity_date text, title text, tags text, answer_count text,
  comment_count text, favorite_count text, closed_date text, community_owned_date text, content_license text);
CREATE UNLOGGED TABLE staging.tags (id text, tag_name text, count text, excerpt_post_id text, wiki_post_id text);
CREATE UNLOGGED TABLE staging.comments (id text, post_id text, score text, text text, creation_date text,
  user_display_name text, user_id text);
CREATE UNLOGGED TABLE staging.votes (id text, post_id text, vote_type_id text, user_id text, bounty_amount text,
  creation_date text);
CREATE UNLOGGED TABLE staging.badges (id text, user_id text, name text, date text, class text, tag_based text);
CREATE UNLOGGED TABLE staging.post_history (id text, post_history_type_id text, post_id text, revision_guid text,
  creation_date text, user_id text, user_display_name text, comment text, text text, content_license text);
CREATE UNLOGGED TABLE staging.post_links (id text, creation_date text, post_id text, related_post_id text,
  link_type_id text);

COPY staging.users FROM PROGRAM '7zz e -so /seed/dump.7z Users.xml | python3 /seed/xml2csv.py Users' (FORMAT csv, HEADER true);
COPY staging.posts FROM PROGRAM '7zz e -so /seed/dump.7z Posts.xml | python3 /seed/xml2csv.py Posts' (FORMAT csv, HEADER true);
COPY staging.tags FROM PROGRAM '7zz e -so /seed/dump.7z Tags.xml | python3 /seed/xml2csv.py Tags' (FORMAT csv, HEADER true);
COPY staging.comments FROM PROGRAM '7zz e -so /seed/dump.7z Comments.xml | python3 /seed/xml2csv.py Comments' (FORMAT csv, HEADER true);
COPY staging.votes FROM PROGRAM '7zz e -so /seed/dump.7z Votes.xml | python3 /seed/xml2csv.py Votes' (FORMAT csv, HEADER true);
COPY staging.badges FROM PROGRAM '7zz e -so /seed/dump.7z Badges.xml | python3 /seed/xml2csv.py Badges' (FORMAT csv, HEADER true);
COPY staging.post_history FROM PROGRAM '7zz e -so /seed/dump.7z PostHistory.xml | python3 /seed/xml2csv.py PostHistory' (FORMAT csv, HEADER true);
COPY staging.post_links FROM PROGRAM '7zz e -so /seed/dump.7z PostLinks.xml | python3 /seed/xml2csv.py PostLinks' (FORMAT csv, HEADER true);

-- ---- Lookups, from Stack Exchange's schema documentation --------------------------------------------------------------

CREATE TABLE post_types (
  id smallint PRIMARY KEY,
  name text NOT NULL
);
INSERT INTO post_types VALUES
  (1, 'Question'), (2, 'Answer'), (3, 'Orphaned tag wiki'), (4, 'Tag wiki excerpt'), (5, 'Tag wiki'),
  (6, 'Moderator nomination'), (7, 'Wiki placeholder'), (8, 'Privilege wiki'), (9, 'Article'), (10, 'HelpArticle'),
  (12, 'Collection'), (13, 'ModeratorQuestionnaireResponse'), (14, 'Announcement'), (15, 'CollectiveDiscussion'),
  (17, 'CollectiveCollection');

CREATE TABLE vote_types (
  id smallint PRIMARY KEY,
  name text NOT NULL,
  description text
);
INSERT INTO vote_types VALUES
  (1, 'AcceptedByOriginator', 'The question''s author accepted this answer'),
  (2, 'UpMod', 'Upvote'),
  (3, 'DownMod', 'Downvote'),
  (4, 'Offensive', 'Flagged as offensive'),
  (5, 'Favorite', 'Bookmark (user_id is set); replaced by Saves after October 2022'),
  (6, 'Close', 'Vote to close; since 2013-06-25 close votes are only recorded in post_history'),
  (7, 'Reopen', 'Vote to reopen'),
  (8, 'BountyStart', 'Bounty offered (user_id and bounty_amount are set)'),
  (9, 'BountyClose', 'Bounty awarded or ended (bounty_amount is usually set)'),
  (10, 'Deletion', 'Vote to delete'),
  (11, 'Undeletion', 'Vote to undelete'),
  (12, 'Spam', 'Flagged as spam'),
  (13, 'InformModerator', 'Flagged for moderator attention'),
  (15, 'ModeratorReview', 'A moderator looked at a flagged post'),
  (16, 'ApproveEditSuggestion', 'Vote to approve a suggested edit');

CREATE TABLE post_history_types (
  id smallint PRIMARY KEY,
  name text NOT NULL,
  description text
);
INSERT INTO post_history_types VALUES
  (1, 'Initial Title', 'Initial title (questions only)'),
  (2, 'Initial Body', 'Initial post raw body text'),
  (3, 'Initial Tags', 'Initial list of tags (questions only)'),
  (4, 'Edit Title', 'Modified title (questions only)'),
  (5, 'Edit Body', 'Modified post body (raw markdown)'),
  (6, 'Edit Tags', 'Modified list of tags (questions only)'),
  (7, 'Rollback Title', 'Reverted title (questions only)'),
  (8, 'Rollback Body', 'Reverted body (raw markdown)'),
  (9, 'Rollback Tags', 'Reverted list of tags (questions only)'),
  (10, 'Post Closed', 'Post voted to be closed'),
  (11, 'Post Reopened', 'Post voted to be reopened'),
  (12, 'Post Deleted', 'Post voted to be removed'),
  (13, 'Post Undeleted', 'Post voted to be restored'),
  (14, 'Post Locked', 'Post locked by a moderator'),
  (15, 'Post Unlocked', 'Post unlocked by a moderator'),
  (16, 'Community Owned', 'Post now community owned'),
  (17, 'Post Migrated', 'Post migrated; replaced by 35 and 36'),
  (18, 'Question Merged', 'Question merged with a deleted question'),
  (19, 'Question Protected', 'Question protected by a moderator'),
  (20, 'Question Unprotected', 'Question unprotected by a moderator'),
  (21, 'Post Disassociated', 'Owner removed from the post by an admin'),
  (22, 'Question Unmerged', 'Answers and votes restored to a previously merged question'),
  (24, 'Suggested Edit Applied', NULL),
  (25, 'Post Tweeted', NULL),
  (31, 'Comment discussion moved to chat', NULL),
  (33, 'Post notice added', NULL),
  (34, 'Post notice removed', NULL),
  (35, 'Post migrated away', NULL),
  (36, 'Post migrated here', NULL),
  (37, 'Post merge source', NULL),
  (38, 'Post merge destination', NULL),
  (50, 'Bumped by Community User', NULL),
  (52, 'Question became hot network question', NULL),
  (53, 'Question removed from hot network questions by a moderator', NULL),
  (66, 'Created from Ask Wizard', NULL);

-- ---- Content ----------------------------------------------------------------------------------------------------------

CREATE TABLE users (
  id integer PRIMARY KEY,
  account_id integer,
  display_name text NOT NULL,
  reputation integer NOT NULL,
  creation_date timestamp NOT NULL,
  last_access_date timestamp NOT NULL,
  location text,
  website_url text,
  about_me text,
  views integer NOT NULL,
  up_votes integer NOT NULL,
  down_votes integer NOT NULL
);
INSERT INTO users
SELECT id::integer, account_id::integer, display_name, reputation::integer, creation_date::timestamp,
       last_access_date::timestamp, location, website_url, about_me, views::integer, up_votes::integer,
       down_votes::integer
FROM staging.users;

CREATE TABLE posts (
  id integer PRIMARY KEY,
  post_type_id smallint NOT NULL,
  parent_id integer,
  accepted_answer_id integer,
  title text,
  body text,
  tags text,
  score integer NOT NULL,
  view_count integer,
  answer_count integer,
  comment_count integer,
  favorite_count integer,
  owner_user_id integer,
  owner_display_name text,
  last_editor_user_id integer,
  last_editor_display_name text,
  creation_date timestamp NOT NULL,
  last_edit_date timestamp,
  last_activity_date timestamp NOT NULL,
  closed_date timestamp,
  community_owned_date timestamp,
  content_license text NOT NULL
);
INSERT INTO posts
SELECT id::integer, post_type_id::smallint, parent_id::integer, accepted_answer_id::integer, title, body, tags,
       score::integer, view_count::integer, answer_count::integer, comment_count::integer, favorite_count::integer,
       owner_user_id::integer, owner_display_name, last_editor_user_id::integer, last_editor_display_name,
       creation_date::timestamp, last_edit_date::timestamp, last_activity_date::timestamp, closed_date::timestamp,
       community_owned_date::timestamp, content_license
FROM staging.posts;

CREATE TABLE tags (
  id integer PRIMARY KEY,
  tag_name text NOT NULL UNIQUE,
  count integer NOT NULL,
  excerpt_post_id integer,
  wiki_post_id integer
);
INSERT INTO tags
SELECT id::integer, tag_name, count::integer, excerpt_post_id::integer, wiki_post_id::integer
FROM staging.tags;

-- posts.tags holds each question's tags as '|mysql|innodb|'; every name matches a tag.
CREATE TABLE post_tags (
  post_id integer NOT NULL,
  tag_id integer NOT NULL,
  PRIMARY KEY (post_id, tag_id)
);
INSERT INTO post_tags
SELECT p.id, t.id
FROM posts p
CROSS JOIN LATERAL unnest(string_to_array(trim(BOTH '|' FROM p.tags), '|')) AS name
JOIN tags t ON t.tag_name = name
WHERE p.tags IS NOT NULL;

CREATE TABLE comments (
  id integer PRIMARY KEY,
  post_id integer NOT NULL,
  user_id integer,
  user_display_name text,
  score integer NOT NULL,
  text text NOT NULL,
  creation_date timestamp NOT NULL
);
INSERT INTO comments
SELECT id::integer, post_id::integer, user_id::integer, user_display_name, score::integer, text,
       creation_date::timestamp
FROM staging.comments;

CREATE TABLE votes (
  id integer PRIMARY KEY,
  post_id integer NOT NULL,
  vote_type_id smallint NOT NULL,
  user_id integer,
  bounty_amount integer,
  creation_date date NOT NULL
);
INSERT INTO votes
SELECT v.id::integer, v.post_id::integer, v.vote_type_id::smallint, v.user_id::integer, v.bounty_amount::integer,
       v.creation_date::timestamp::date
FROM staging.votes v;

CREATE TABLE badges (
  id integer PRIMARY KEY,
  user_id integer NOT NULL,
  name text NOT NULL,
  class smallint NOT NULL,
  tag_based boolean NOT NULL,
  date timestamp NOT NULL
);
INSERT INTO badges
SELECT id::integer, user_id::integer, name, class::smallint, tag_based::boolean, date::timestamp
FROM staging.badges;

CREATE TABLE post_history (
  id integer PRIMARY KEY,
  post_id integer NOT NULL,
  post_history_type_id smallint NOT NULL,
  revision_guid uuid NOT NULL,
  user_id integer,
  user_display_name text,
  comment text,
  text text,
  content_license text,
  creation_date timestamp NOT NULL
);
INSERT INTO post_history
SELECT h.id::integer, h.post_id::integer, h.post_history_type_id::smallint, h.revision_guid::uuid, h.user_id::integer,
       h.user_display_name, h.comment, h.text, h.content_license, h.creation_date::timestamp
FROM staging.post_history h;

CREATE TABLE post_links (
  id integer PRIMARY KEY,
  post_id integer NOT NULL,
  related_post_id integer NOT NULL,
  link_type_id smallint NOT NULL,
  creation_date timestamp NOT NULL
);
INSERT INTO post_links
SELECT l.id::integer, l.post_id::integer, l.related_post_id::integer, l.link_type_id::smallint,
       l.creation_date::timestamp
FROM staging.post_links l;

DROP SCHEMA staging CASCADE;

-- ---- Keys and indexes -------------------------------------------------------------------------------------------------

-- NOT VALID where the dump has rows pointing at deleted posts or users (see the top).
ALTER TABLE posts
  ADD FOREIGN KEY (post_type_id) REFERENCES post_types,
  ADD FOREIGN KEY (parent_id) REFERENCES posts,
  ADD FOREIGN KEY (accepted_answer_id) REFERENCES posts NOT VALID,
  ADD FOREIGN KEY (owner_user_id) REFERENCES users NOT VALID,
  ADD FOREIGN KEY (last_editor_user_id) REFERENCES users;
ALTER TABLE tags
  ADD FOREIGN KEY (excerpt_post_id) REFERENCES posts,
  ADD FOREIGN KEY (wiki_post_id) REFERENCES posts;
ALTER TABLE post_tags
  ADD FOREIGN KEY (post_id) REFERENCES posts,
  ADD FOREIGN KEY (tag_id) REFERENCES tags;
ALTER TABLE comments
  ADD FOREIGN KEY (post_id) REFERENCES posts,
  ADD FOREIGN KEY (user_id) REFERENCES users;
ALTER TABLE votes
  ADD FOREIGN KEY (post_id) REFERENCES posts NOT VALID,
  ADD FOREIGN KEY (vote_type_id) REFERENCES vote_types,
  ADD FOREIGN KEY (user_id) REFERENCES users;
ALTER TABLE badges
  ADD FOREIGN KEY (user_id) REFERENCES users;
ALTER TABLE post_history
  ADD FOREIGN KEY (post_id) REFERENCES posts NOT VALID,
  ADD FOREIGN KEY (post_history_type_id) REFERENCES post_history_types,
  ADD FOREIGN KEY (user_id) REFERENCES users;
ALTER TABLE post_links
  ADD FOREIGN KEY (post_id) REFERENCES posts,
  ADD FOREIGN KEY (related_post_id) REFERENCES posts NOT VALID;

CREATE INDEX ON posts (post_type_id);
CREATE INDEX ON posts (parent_id);
CREATE INDEX ON posts (owner_user_id);
CREATE INDEX ON posts (creation_date);
CREATE INDEX ON post_tags (tag_id);
CREATE INDEX ON comments (post_id);
CREATE INDEX ON comments (user_id);
CREATE INDEX ON comments (creation_date);
CREATE INDEX ON votes (post_id);
CREATE INDEX ON votes (vote_type_id);
CREATE INDEX ON votes (creation_date);
CREATE INDEX ON badges (user_id);
CREATE INDEX ON badges (date);
CREATE INDEX ON post_history (post_id);
CREATE INDEX ON post_history (user_id);
CREATE INDEX ON post_links (post_id);
CREATE INDEX ON post_links (related_post_id);
CREATE INDEX ON users (creation_date);

-- ---- Documentation (Metabase shows these as table and column descriptions) -------------------------------------------

COMMENT ON DATABASE stackexchange IS 'dba.stackexchange.com (Database Administrators Stack Exchange), from the Stack Exchange data dump of 2024-04-06: all activity from the site''s launch in January 2011 to 2024-03-31, plus a few hundred posts migrated from Stack Overflow back to 2008. Per-post and per-user state (scores, counts, last activity, last access) is as of 2024-04-06. Timestamps are UTC. Content by Stack Exchange users, licensed CC BY-SA (see content_license).';

COMMENT ON TABLE users IS 'Every user account on the site, including the Community user (id -1), a background process that owns tag wikis and bumps old questions.';
COMMENT ON COLUMN users.id IS 'The user''s id on this site.';
COMMENT ON COLUMN users.account_id IS 'The user''s Stack Exchange network account id, shared across all Stack Exchange sites. NULL if the user hides this community on their profile.';
COMMENT ON COLUMN users.display_name IS 'The user''s public name.';
COMMENT ON COLUMN users.reputation IS 'Reputation points at the time of the dump.';
COMMENT ON COLUMN users.creation_date IS 'When the account was created.';
COMMENT ON COLUMN users.last_access_date IS 'When the user last loaded a page (updated every 30 minutes at most).';
COMMENT ON COLUMN users.location IS 'Free-text location from the user''s profile.';
COMMENT ON COLUMN users.website_url IS 'Website from the user''s profile.';
COMMENT ON COLUMN users.about_me IS 'The "about me" text of the user''s profile, as HTML.';
COMMENT ON COLUMN users.views IS 'Number of times the user''s profile was viewed.';
COMMENT ON COLUMN users.up_votes IS 'Number of upvotes the user has cast.';
COMMENT ON COLUMN users.down_votes IS 'Number of downvotes the user has cast.';

COMMENT ON TABLE posts IS 'Every non-deleted post: questions (post_type_id 1), answers (2), tag wikis and their excerpts (5, 4), and a few moderator nominations and site pages.';
COMMENT ON COLUMN posts.id IS 'The post''s id; https://dba.stackexchange.com/q/<id> links to it.';
COMMENT ON COLUMN posts.post_type_id IS 'The kind of post: 1 question, 2 answer, 4 tag wiki excerpt, 5 tag wiki, 6 moderator nomination, 7 wiki placeholder.';
COMMENT ON COLUMN posts.parent_id IS 'For an answer, the question it answers.';
COMMENT ON COLUMN posts.accepted_answer_id IS 'For a question, the answer its author accepted, if any. In one case that answer was deleted.';
COMMENT ON COLUMN posts.title IS 'The question''s title (questions only).';
COMMENT ON COLUMN posts.body IS 'The post''s content, as rendered HTML.';
COMMENT ON COLUMN posts.tags IS 'The question''s tags, as ''|tag1|tag2|'' (questions only). post_tags has them one per row.';
COMMENT ON COLUMN posts.score IS 'Upvotes minus downvotes.';
COMMENT ON COLUMN posts.view_count IS 'Number of times the question was viewed (questions only).';
COMMENT ON COLUMN posts.answer_count IS 'Number of non-deleted answers (questions only).';
COMMENT ON COLUMN posts.comment_count IS 'Number of comments on the post.';
COMMENT ON COLUMN posts.favorite_count IS 'Number of users who bookmarked the question, when recorded.';
COMMENT ON COLUMN posts.owner_user_id IS 'The author. NULL if their account was deleted (owner_display_name then holds their name). -1 is the Community user, which owns some tag wikis.';
COMMENT ON COLUMN posts.owner_display_name IS 'The author''s name, set when the author is a deleted or anonymous user, or the original author of a post migrated from another site.';
COMMENT ON COLUMN posts.last_editor_user_id IS 'The user who edited the post last.';
COMMENT ON COLUMN posts.last_editor_display_name IS 'The last editor''s name, set when the editor is a deleted or anonymous user.';
COMMENT ON COLUMN posts.creation_date IS 'When the post was created.';
COMMENT ON COLUMN posts.last_edit_date IS 'When the post was last edited.';
COMMENT ON COLUMN posts.last_activity_date IS 'When anything last happened on the post (an edit, an answer, a comment...).';
COMMENT ON COLUMN posts.closed_date IS 'When the question was closed, if it is closed.';
COMMENT ON COLUMN posts.community_owned_date IS 'When the post became community wiki, if it did.';
COMMENT ON COLUMN posts.content_license IS 'The Creative Commons license the content is under: CC BY-SA 2.5 until 2011-04-07, 3.0 until 2018-05-01, 4.0 after.';

COMMENT ON TABLE post_types IS 'The kinds of post (posts.post_type_id), from Stack Exchange''s schema documentation.';
COMMENT ON TABLE tags IS 'The tags questions are labeled with.';
COMMENT ON COLUMN tags.tag_name IS 'The tag, e.g. sql-server, postgresql, mysql.';
COMMENT ON COLUMN tags.count IS 'Number of questions with the tag, as counted by Stack Exchange at the time of the dump.';
COMMENT ON COLUMN tags.excerpt_post_id IS 'The post holding the tag''s short description (its tag wiki excerpt).';
COMMENT ON COLUMN tags.wiki_post_id IS 'The post holding the tag''s wiki.';
COMMENT ON TABLE post_tags IS 'Which tags each question has: one row per question and tag, split out of posts.tags.';

COMMENT ON TABLE comments IS 'Comments on questions and answers.';
COMMENT ON COLUMN comments.post_id IS 'The question or answer commented on.';
COMMENT ON COLUMN comments.user_id IS 'The commenter. NULL if their account was deleted (user_display_name then holds their name).';
COMMENT ON COLUMN comments.user_display_name IS 'The commenter''s name, set when the commenter''s account was deleted.';
COMMENT ON COLUMN comments.score IS 'Number of upvotes on the comment.';
COMMENT ON COLUMN comments.text IS 'The comment, as markdown.';

COMMENT ON TABLE votes IS 'Votes on posts: upvotes, downvotes, accepted answers, bounties, close, delete, spam and moderator votes. Anonymous: user_id is only set for bookmarks and bounties. 111,161 votes are on posts that were later deleted, so their post_id matches no post.';
COMMENT ON COLUMN votes.post_id IS 'The post voted on.';
COMMENT ON COLUMN votes.vote_type_id IS 'The kind of vote; see vote_types.';
COMMENT ON COLUMN votes.user_id IS 'The voter, only for bookmarks (5) and bounty starts (8). -1 when the voter''s account was deleted, which is also the Community user''s id.';
COMMENT ON COLUMN votes.bounty_amount IS 'Reputation offered or awarded, only for bounty votes (8 and 9).';
COMMENT ON COLUMN votes.creation_date IS 'The day of the vote (Stack Exchange removes the time of day for privacy).';
COMMENT ON TABLE vote_types IS 'The kinds of vote (votes.vote_type_id), from the data dump''s documentation.';

COMMENT ON TABLE badges IS 'Badges awarded to users.';
COMMENT ON COLUMN badges.name IS 'The badge, e.g. Teacher, Nice Answer, or a tag for tag badges.';
COMMENT ON COLUMN badges.class IS '1 gold, 2 silver, 3 bronze.';
COMMENT ON COLUMN badges.tag_based IS 'Whether the badge is for a tag (name is then the tag), rather than a named badge.';
COMMENT ON COLUMN badges.date IS 'When the badge was awarded.';

COMMENT ON TABLE post_history IS 'Every revision and moderation event on posts: initial versions, edits, rollbacks, closures, reopenings, deletions, locks, migrations. One action can record several rows sharing a revision_guid. 22 rows are on posts that were later deleted.';
COMMENT ON COLUMN post_history.post_history_type_id IS 'The kind of event; see post_history_types.';
COMMENT ON COLUMN post_history.revision_guid IS 'Groups the rows recorded by a single action.';
COMMENT ON COLUMN post_history.user_id IS 'Who made the change. NULL if their account was deleted (user_display_name then holds their name), and for some system events.';
COMMENT ON COLUMN post_history.user_display_name IS 'Who made the change, when their account was deleted, and the author of a migrated post.';
COMMENT ON COLUMN post_history.comment IS 'The editor''s summary of the change. For closures (10), the close reason: 101 duplicate, 102 off-topic, 103 unclear, 104 too broad, 105 opinion-based (1-20 are older reasons).';
COMMENT ON COLUMN post_history.text IS 'The new value: the markdown body for body events (2, 5, 8), the title or tags for title and tag events, a JSON list of voters for closures, reopenings, deletions, locks and protections, migration details for migrations.';
COMMENT ON COLUMN post_history.content_license IS 'The Creative Commons license of the revision''s content.';
COMMENT ON TABLE post_history_types IS 'The kinds of post history event (post_history.post_history_type_id), from Stack Exchange''s schema documentation.';

COMMENT ON TABLE post_links IS 'Links between questions: a question linking to another (link_type_id 1) or closed as a duplicate of another (3). 290 links point at questions that were later deleted.';
COMMENT ON COLUMN post_links.post_id IS 'The linking question, or the duplicate.';
COMMENT ON COLUMN post_links.related_post_id IS 'The linked question, or the original the duplicate points to.';
COMMENT ON COLUMN post_links.link_type_id IS '1 Linked (post_id links to related_post_id), 3 Duplicate (post_id is a duplicate of related_post_id).';

-- ---- The build fails unless the load matches the pinned dump -----------------------------------------------------------

DO $$
DECLARE
  expected CONSTANT jsonb := '{"users": 248141, "posts": 243410, "tags": 1242, "post_tags": 278266, "comments": 347838,
    "votes": 911783, "badges": 429421, "post_history": 833657, "post_links": 20194}';
  name text;
  actual bigint;
BEGIN
  FOR name IN SELECT jsonb_object_keys(expected) LOOP
    EXECUTE format('SELECT count(*) FROM %I', name) INTO actual;
    IF actual <> (expected ->> name)::bigint THEN
      RAISE EXCEPTION '% has % rows, expected %', name, actual, expected ->> name;
    END IF;
  END LOOP;
END $$;
