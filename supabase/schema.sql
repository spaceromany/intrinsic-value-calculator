-- 게시물 테이블
create table posts (
    id uuid default uuid_generate_v4() primary key,
    content text not null,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null,
    stocks jsonb not null default '[]'::jsonb,
    likes integer default 0,
    shares integer default 0
);

-- 댓글 테이블
create table comments (
    id uuid default uuid_generate_v4() primary key,
    post_id uuid references posts(id) on delete cascade,
    content text not null,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null,
    likes integer default 0
);

-- 좋아요 테이블 (익명 사용자용)
create table likes (
    id uuid default uuid_generate_v4() primary key,
    post_id uuid references posts(id) on delete cascade,
    comment_id uuid references comments(id) on delete cascade,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null,
    ip_hash text not null,
    constraint likes_post_or_comment check (
        (post_id is not null and comment_id is null) or
        (post_id is null and comment_id is not null)
    )
);

-- RLS (Row Level Security) 정책 설정
alter table posts enable row level security;
alter table comments enable row level security;
alter table likes enable row level security;

-- 게시물 정책
create policy "게시물 조회는 모두 가능" on posts
    for select using (true);

create policy "게시물 작성은 모두 가능" on posts
    for insert with check (true);

-- 댓글 정책
create policy "댓글 조회는 모두 가능" on comments
    for select using (true);

create policy "댓글 작성은 모두 가능" on comments
    for insert with check (true);

-- 좋아요 정책
create policy "좋아요 조회는 모두 가능" on likes
    for select using (true);

create policy "좋아요 작성은 모두 가능" on likes
    for insert with check (true);

-- 인덱스 생성
create index posts_created_at_idx on posts(created_at desc);
create index comments_post_id_idx on comments(post_id);
create index likes_post_id_idx on likes(post_id);
create index likes_comment_id_idx on likes(comment_id);
create index likes_ip_hash_idx on likes(ip_hash); 