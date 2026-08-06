"""Emit a realistic large Prisma schema (publishing platform / SaaS CMS).

Unlike benchmarks/gen_schema.py (a synthetic chain), this is a hand-shaped graph:
~45 models, mixed 1-1 / 1-N / N-M relations, self-relations, enums, Json/Bytes/
Decimal columns — the shape a real application schema has.

The generator block is parameterized so the optimization loop can re-emit the
same datamodel under different generator options:

    python schema_gen.py --interface asyncio --output ./pkg_async \
        --schema ./async.prisma --option minimalRuntime=true \
        --option recursive_type_depth=-1
"""

from __future__ import annotations

import argparse
from pathlib import Path

ENUMS = """
enum Role {
  OWNER
  ADMIN
  EDITOR
  AUTHOR
  VIEWER
}

enum PostStatus {
  DRAFT
  IN_REVIEW
  SCHEDULED
  PUBLISHED
  ARCHIVED
}

enum SubscriptionTier {
  FREE
  PRO
  TEAM
  ENTERPRISE
}

enum InvoiceStatus {
  OPEN
  PAID
  VOID
  UNCOLLECTIBLE
}

enum NotificationKind {
  MENTION
  REPLY
  FOLLOW
  DIGEST
  BILLING
}
"""

# The datamodel proper. Kept as one literal so the loop regenerates it
# byte-identically; only the generator block varies between iterations.
DATAMODEL = """
model Organization {
  id          String   @id @default(cuid())
  slug        String   @unique
  name        String
  settings    Json?
  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt
  memberships Membership[]
  teams       Team[]
  sites       Site[]
  subscription Subscription?
  invoices    Invoice[]
  auditLogs   AuditLog[]
  apiKeys     ApiKey[]
  webhooks    Webhook[]
}

model User {
  id            String    @id @default(cuid())
  email         String    @unique
  handle        String    @unique
  hashedPassword Bytes?
  createdAt     DateTime  @default(now())
  updatedAt     DateTime  @updatedAt
  profile       Profile?
  memberships   Membership[]
  sessions      AuthSession[]
  posts         Post[]
  comments      Comment[]
  reactions     Reaction[]
  mediaAssets   MediaAsset[]
  notifications Notification[] @relation("recipient")
  sentNotifications Notification[] @relation("actor")
  followers     Follow[]  @relation("followee")
  following     Follow[]  @relation("follower")
  bookmarks     Bookmark[]
  reviewsAssigned ReviewRequest[] @relation("reviewer")
  reviewsRequested ReviewRequest[] @relation("requester")
  apiKeys       ApiKey[]
  auditLogs     AuditLog[]
}

model Profile {
  id        String  @id @default(cuid())
  userId    String  @unique
  user      User    @relation(fields: [userId], references: [id], onDelete: Cascade)
  bio       String?
  avatarUrl String?
  location  String?
  links     Json?
}

model AuthSession {
  id        String   @id @default(cuid())
  userId    String
  user      User     @relation(fields: [userId], references: [id], onDelete: Cascade)
  token     String   @unique
  userAgent String?
  ip        String?
  expiresAt DateTime
  createdAt DateTime @default(now())
}

model Membership {
  id     String @id @default(cuid())
  orgId  String
  org    Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)
  userId String
  user   User   @relation(fields: [userId], references: [id], onDelete: Cascade)
  role   Role   @default(VIEWER)
  teams  TeamMember[]
  @@unique([orgId, userId])
}

model Team {
  id      String @id @default(cuid())
  orgId   String
  org     Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)
  name    String
  members TeamMember[]
  sites   Site[]
  @@unique([orgId, name])
}

model TeamMember {
  id           String @id @default(cuid())
  teamId       String
  team         Team @relation(fields: [teamId], references: [id], onDelete: Cascade)
  membershipId String
  membership   Membership @relation(fields: [membershipId], references: [id], onDelete: Cascade)
  @@unique([teamId, membershipId])
}

model Site {
  id        String  @id @default(cuid())
  orgId     String
  org       Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)
  teamId    String?
  team      Team?   @relation(fields: [teamId], references: [id])
  domain    String  @unique
  title     String
  theme     Json?
  posts     Post[]
  pages     Page[]
  categories Category[]
  redirects Redirect[]
  navMenus  NavMenu[]
}

model Post {
  id          String     @id @default(cuid())
  siteId      String
  site        Site       @relation(fields: [siteId], references: [id], onDelete: Cascade)
  authorId    String
  author      User       @relation(fields: [authorId], references: [id])
  slug        String
  title       String
  status      PostStatus @default(DRAFT)
  publishedAt DateTime?
  createdAt   DateTime   @default(now())
  updatedAt   DateTime   @updatedAt
  revisions   PostRevision[]
  currentRevisionId String? @unique
  currentRevision   PostRevision? @relation("current", fields: [currentRevisionId], references: [id])
  comments    Comment[]
  tags        PostTag[]
  categoryId  String?
  category    Category?  @relation(fields: [categoryId], references: [id])
  reactions   Reaction[]
  bookmarks   Bookmark[]
  reviewRequests ReviewRequest[]
  seo         SeoMeta?
  series      SeriesEntry[]
  coverId     String?
  cover       MediaAsset? @relation(fields: [coverId], references: [id])
  stats       PostStats?
  @@unique([siteId, slug])
}

model PostRevision {
  id        String   @id @default(cuid())
  postId    String
  post      Post     @relation(fields: [postId], references: [id], onDelete: Cascade)
  currentOf Post?    @relation("current")
  body      String
  summary   String?
  wordCount Int      @default(0)
  createdAt DateTime @default(now())
  embeds    Embed[]
}

model Embed {
  id         String @id @default(cuid())
  revisionId String
  revision   PostRevision @relation(fields: [revisionId], references: [id], onDelete: Cascade)
  kind       String
  payload    Json
  position   Int
}

model Page {
  id     String @id @default(cuid())
  siteId String
  site   Site   @relation(fields: [siteId], references: [id], onDelete: Cascade)
  slug   String
  title  String
  body   String
  seo    SeoMeta?
  @@unique([siteId, slug])
}

model SeoMeta {
  id          String  @id @default(cuid())
  postId      String? @unique
  post        Post?   @relation(fields: [postId], references: [id], onDelete: Cascade)
  pageId      String? @unique
  page        Page?   @relation(fields: [pageId], references: [id], onDelete: Cascade)
  title       String?
  description String?
  ogImageUrl  String?
  noindex     Boolean @default(false)
}

model Category {
  id       String  @id @default(cuid())
  siteId   String
  site     Site    @relation(fields: [siteId], references: [id], onDelete: Cascade)
  parentId String?
  parent   Category?  @relation("tree", fields: [parentId], references: [id])
  children Category[] @relation("tree")
  name     String
  posts    Post[]
  @@unique([siteId, name])
}

model Tag {
  id    String    @id @default(cuid())
  name  String    @unique
  posts PostTag[]
}

model PostTag {
  postId String
  post   Post   @relation(fields: [postId], references: [id], onDelete: Cascade)
  tagId  String
  tag    Tag    @relation(fields: [tagId], references: [id], onDelete: Cascade)
  @@id([postId, tagId])
}

model Series {
  id      String        @id @default(cuid())
  title   String        @unique
  entries SeriesEntry[]
}

model SeriesEntry {
  seriesId String
  series   Series @relation(fields: [seriesId], references: [id], onDelete: Cascade)
  postId   String
  post     Post   @relation(fields: [postId], references: [id], onDelete: Cascade)
  position Int
  @@id([seriesId, postId])
}

model Comment {
  id        String    @id @default(cuid())
  postId    String
  post      Post      @relation(fields: [postId], references: [id], onDelete: Cascade)
  authorId  String
  author    User      @relation(fields: [authorId], references: [id])
  parentId  String?
  parent    Comment?  @relation("thread", fields: [parentId], references: [id])
  replies   Comment[] @relation("thread")
  body      String
  isHidden  Boolean   @default(false)
  createdAt DateTime  @default(now())
  reactions Reaction[]
}

model Reaction {
  id        String   @id @default(cuid())
  userId    String
  user      User     @relation(fields: [userId], references: [id], onDelete: Cascade)
  postId    String?
  post      Post?    @relation(fields: [postId], references: [id], onDelete: Cascade)
  commentId String?
  comment   Comment? @relation(fields: [commentId], references: [id], onDelete: Cascade)
  emoji     String
  createdAt DateTime @default(now())
  @@unique([userId, postId, commentId, emoji])
}

model Follow {
  followerId String
  follower   User @relation("follower", fields: [followerId], references: [id], onDelete: Cascade)
  followeeId String
  followee   User @relation("followee", fields: [followeeId], references: [id], onDelete: Cascade)
  createdAt  DateTime @default(now())
  @@id([followerId, followeeId])
}

model Bookmark {
  userId    String
  user      User   @relation(fields: [userId], references: [id], onDelete: Cascade)
  postId    String
  post      Post   @relation(fields: [postId], references: [id], onDelete: Cascade)
  createdAt DateTime @default(now())
  @@id([userId, postId])
}

model ReviewRequest {
  id          String   @id @default(cuid())
  postId      String
  post        Post     @relation(fields: [postId], references: [id], onDelete: Cascade)
  requesterId String
  requester   User     @relation("requester", fields: [requesterId], references: [id])
  reviewerId  String
  reviewer    User     @relation("reviewer", fields: [reviewerId], references: [id])
  note        String?
  resolvedAt  DateTime?
  createdAt   DateTime @default(now())
}

model MediaAsset {
  id         String  @id @default(cuid())
  uploaderId String
  uploader   User    @relation(fields: [uploaderId], references: [id])
  url        String
  mimeType   String
  sizeBytes  BigInt
  width      Int?
  height     Int?
  blurhash   String?
  exif       Json?
  posts      Post[]
  variants   MediaVariant[]
}

model MediaVariant {
  id       String @id @default(cuid())
  assetId  String
  asset    MediaAsset @relation(fields: [assetId], references: [id], onDelete: Cascade)
  label    String
  url      String
  width    Int
  height   Int
  @@unique([assetId, label])
}

model NavMenu {
  id     String @id @default(cuid())
  siteId String
  site   Site   @relation(fields: [siteId], references: [id], onDelete: Cascade)
  name   String
  items  NavMenuItem[]
  @@unique([siteId, name])
}

model NavMenuItem {
  id       String  @id @default(cuid())
  menuId   String
  menu     NavMenu @relation(fields: [menuId], references: [id], onDelete: Cascade)
  parentId String?
  parent   NavMenuItem?  @relation("tree", fields: [parentId], references: [id])
  children NavMenuItem[] @relation("tree")
  label    String
  href     String
  position Int
}

model Redirect {
  id       String @id @default(cuid())
  siteId   String
  site     Site   @relation(fields: [siteId], references: [id], onDelete: Cascade)
  fromPath String
  toPath   String
  code     Int    @default(301)
  @@unique([siteId, fromPath])
}

model Subscription {
  id        String @id @default(cuid())
  orgId     String @unique
  org       Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)
  tier      SubscriptionTier @default(FREE)
  seats     Int    @default(1)
  renewsAt  DateTime?
  metadata  Json?
}

model Invoice {
  id        String @id @default(cuid())
  orgId     String
  org       Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)
  number    String @unique
  status    InvoiceStatus @default(OPEN)
  currency  String @default("usd")
  total     Decimal
  issuedAt  DateTime @default(now())
  paidAt    DateTime?
  lines     InvoiceLine[]
  payments  Payment[]
}

model InvoiceLine {
  id        String  @id @default(cuid())
  invoiceId String
  invoice   Invoice @relation(fields: [invoiceId], references: [id], onDelete: Cascade)
  description String
  quantity  Int     @default(1)
  unitPrice Decimal
}

model Payment {
  id        String  @id @default(cuid())
  invoiceId String
  invoice   Invoice @relation(fields: [invoiceId], references: [id], onDelete: Cascade)
  provider  String
  reference String  @unique
  amount    Decimal
  createdAt DateTime @default(now())
}

model Notification {
  id          String @id @default(cuid())
  recipientId String
  recipient   User   @relation("recipient", fields: [recipientId], references: [id], onDelete: Cascade)
  actorId     String?
  actor       User?  @relation("actor", fields: [actorId], references: [id])
  kind        NotificationKind
  payload     Json?
  readAt      DateTime?
  createdAt   DateTime @default(now())
}

model ApiKey {
  id        String  @id @default(cuid())
  orgId     String?
  org       Organization? @relation(fields: [orgId], references: [id], onDelete: Cascade)
  userId    String?
  user      User?   @relation(fields: [userId], references: [id], onDelete: Cascade)
  name      String
  hash      Bytes
  scopes    String[]
  lastUsedAt DateTime?
  createdAt DateTime @default(now())
}

model Webhook {
  id        String @id @default(cuid())
  orgId     String
  org       Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)
  url       String
  secret    String
  events    String[]
  isActive  Boolean @default(true)
  deliveries WebhookDelivery[]
}

model WebhookDelivery {
  id         String  @id @default(cuid())
  webhookId  String
  webhook    Webhook @relation(fields: [webhookId], references: [id], onDelete: Cascade)
  event      String
  statusCode Int?
  requestBody Json
  responseBody String?
  durationMs Int?
  createdAt  DateTime @default(now())
}

model AuditLog {
  id       String @id @default(cuid())
  orgId    String
  org      Organization @relation(fields: [orgId], references: [id], onDelete: Cascade)
  actorId  String?
  actor    User?  @relation(fields: [actorId], references: [id])
  action   String
  target   String?
  metadata Json?
  createdAt DateTime @default(now())
}

model FeatureFlag {
  id        String  @id @default(cuid())
  key       String  @unique
  enabled   Boolean @default(false)
  rules     Json?
  updatedAt DateTime @updatedAt
}

model DailyStat {
  day       DateTime
  siteId    String
  views     Int @default(0)
  visitors  Int @default(0)
  @@id([day, siteId])
}

model PostStats {
  postId    String @id
  post      Post   @relation(fields: [postId], references: [id], onDelete: Cascade)
  views     Int    @default(0)
  reads     Int    @default(0)
  claps     Int    @default(0)
}
"""


def build(interface: str, output: str, options: dict[str, str]) -> str:
    gen_lines = [
        'generator client {',
        '  provider             = "prisma-client-py"',
        f'  interface            = "{interface}"',
        f'  output               = "{output}"',
        # the datamodel uses Decimal columns (Invoice/Payment), which are gated
        '  enable_experimental_decimal = true',
    ]
    for key, value in options.items():
        gen_lines.append(f'  {key} = {value}')
    gen_lines.append('}')

    return '\n'.join(
        [
            'datasource db {',
            '  provider = "postgresql"',
            '  url      = env("BENCH_DATABASE_URL")',
            '}',
            '',
            *gen_lines,
            ENUMS,
            DATAMODEL,
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='asyncio')
    parser.add_argument('--output', required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument(
        '--option',
        action='append',
        default=[],
        help='extra generator option, key=value (value emitted verbatim)',
    )
    args = parser.parse_args()

    options: dict[str, str] = {}
    for raw in args.option:
        key, _, value = raw.partition('=')
        options[key] = value

    Path(args.schema).parent.mkdir(parents=True, exist_ok=True)
    Path(args.schema).write_text(build(args.interface, args.output, options))
    print(f'wrote {args.schema} ({args.interface}, options={options or "none"})')


if __name__ == '__main__':
    main()
