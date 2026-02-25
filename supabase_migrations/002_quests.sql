-- quests: задания от агентов, требующие действия пользователя
create table if not exists quests (
    id           bigint generated always as identity primary key,
    title        text not null,
    description  text not null default '',
    quest_type   text not null check (quest_type in ('provide_token', 'api_key', 'approve', 'top_up', 'info')),
    agent        text not null default '',
    status       text not null default 'pending' check (status in ('pending', 'completed', 'skipped')),
    data         jsonb not null default '{}',
    response     jsonb,
    xp_reward    int not null default 10,
    created_at   timestamptz not null default now(),
    completed_at timestamptz
);

create index if not exists idx_quests_status     on quests (status);
create index if not exists idx_quests_agent      on quests (agent);
create index if not exists idx_quests_created_at on quests (created_at desc);

-- RLS
alter table quests enable row level security;
create policy "anon read quests"   on quests for select using (true);
create policy "anon insert quests" on quests for insert with check (true);
create policy "anon update quests" on quests for update using (true);
