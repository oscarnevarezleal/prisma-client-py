-- Idempotent seed for the pg-lab workload.
-- Small on purpose: latencies measure client+engine overhead per query shape,
-- not database performance at scale.
TRUNCATE "Organization" CASCADE;
TRUNCATE "User" CASCADE;
TRUNCATE "Tag" CASCADE;

INSERT INTO "User" (id, email, handle, "createdAt", "updatedAt")
SELECT 'u'||i, 'user'||i||'@example.com', 'handle'||i, now(), now()
FROM generate_series(1,50) i;

INSERT INTO "Organization" (id, slug, name, "createdAt", "updatedAt")
SELECT 'o'||i, 'org-'||i, 'Org '||i, now(), now()
FROM generate_series(1,10) i;

INSERT INTO "Membership" (id, "orgId", "userId", role)
SELECT 'm'||i, 'o'||(1+(i%10)), 'u'||i, 'AUTHOR'
FROM generate_series(1,50) i;

INSERT INTO "Site" (id, "orgId", domain, title)
SELECT 's'||i, 'o'||(1+(i%10)), 'site'||i||'.example.com', 'Site '||i
FROM generate_series(1,40) i;

INSERT INTO "Post" (id, "siteId", "authorId", slug, title, status, "createdAt", "updatedAt")
SELECT 'p'||i, 's'||(1+(i%40)), 'u'||(1+(i%50)), 'post-'||i, 'Post '||i,
       (ARRAY['DRAFT','PUBLISHED','PUBLISHED','ARCHIVED'])[1+(i%4)]::"PostStatus", now(), now()
FROM generate_series(1,400) i;

INSERT INTO "PostRevision" (id, "postId", body, "wordCount", "createdAt")
SELECT 'r'||i, 'p'||i, repeat('lorem ipsum ', 50), 100, now()
FROM generate_series(1,400) i;

INSERT INTO "Comment" (id, "postId", "authorId", body, "createdAt")
SELECT 'c'||i, 'p'||(1+(i%400)), 'u'||(1+(i%50)), 'comment body '||i, now()
FROM generate_series(1,800) i;

INSERT INTO "Tag" (id, name)
SELECT 't'||i, 'tag-'||i FROM generate_series(1,30) i;

INSERT INTO "PostTag" ("postId","tagId")
SELECT 'p'||i, 't'||(1+(i%30)) FROM generate_series(1,400) i;
