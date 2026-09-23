"""Во что ход обошёлся бы по API: арифметика общая, цены — у провайдеров."""
from decimal import Decimal

from agent_workers.base import Entry, Reply, Usage, estimate, rates
from agent_workers.providers.claude import ClaudeAdapter
from agent_workers.providers.claude import tokens_of as claude_tokens
from agent_workers.providers.codex import CodexAdapter
from agent_workers.providers.codex import tokens_of as codex_tokens

from .stubs import cli_stub


def test_each_kind_of_token_is_priced_separately():
    cost = estimate(Usage(input=1_000_000, cached_input=1_000_000, cache_write=1_000_000,
                          output=1_000_000),
                    rates("5", "25", "0.5", "6.25"))
    assert cost.parts == {"input": Decimal("5.000000"), "cached_input": Decimal("0.500000"),
                          "cache_write": Decimal("6.250000"), "output": Decimal("25.000000")}
    assert cost.amount == Decimal("36.750000")


def test_missing_cache_prices_fall_back_to_the_input_price():
    cost = estimate(Usage(cached_input=1_000_000), rates("5", "25"))
    assert cost.amount == Decimal("5.000000")


def test_free_cache_writes_cost_nothing():
    # У OpenAI запись в кеш не тарифицируется — в таблице это явный ноль, не пропуск.
    cost = estimate(Usage(cache_write=1_000_000), rates("4", "20", "0.4", "0"))
    assert cost.amount == Decimal("0.000000")
    assert cost.parts == {}


def test_claude_tokens_separate_cache_from_fresh_input():
    usage = {"input_tokens": 9, "cache_read_input_tokens": 26002,
             "cache_creation_input_tokens": 9688, "output_tokens": 42,
             "output_tokens_details": {"thinking_tokens": 36}}
    tokens = claude_tokens(usage)
    assert (tokens.input, tokens.cached_input, tokens.cache_write) == (9, 26002, 9688)
    # Рассуждения уже внутри output, поэтому в сумму отдельно не идут.
    assert (tokens.output, tokens.reasoning, tokens.total) == (42, 36, 35741)


def test_claude_uses_the_price_the_cli_reported(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("claude-stream.jsonl"))
    reply = ClaudeAdapter(executable=cli_stub()).reply(entry, profile)
    cost = ClaudeAdapter(executable=cli_stub()).price(reply)
    assert (cost.amount, cost.source) == (Decimal("0.0231"), "cli")


def test_dated_snapshot_resolves_to_the_name_the_table_knows(sample, profile, tmp_path):
    """CLI называет модель со снимком даты, а прайс — по каноническому имени."""
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("claude-stream.jsonl"))
    assert ClaudeAdapter(executable=cli_stub()).reply(entry, profile).model == "claude-opus-5"


def test_claude_falls_back_to_the_table_when_the_cli_is_silent():
    reply = Reply("ok", tokens=Usage(input=1_000_000, output=1_000_000),
                  model="claude-opus-5", usage={})
    cost = ClaudeAdapter(executable=cli_stub()).price(reply)
    assert cost.amount == Decimal("30.000000")
    assert cost.source.startswith("anthropic")


def test_codex_is_priced_from_the_table(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("codex-exec.jsonl"))
    adapter = CodexAdapter(executable=cli_stub())
    reply = adapter.reply(entry, profile)
    assert codex_tokens(reply.usage).input == 18827
    cost = adapter.price(reply)
    # 18827 входных по $4 и 5 выходных по $20 за миллион.
    assert cost.amount == Decimal("0.075408")
    assert cost.source.startswith("openai")


def test_unknown_model_gives_no_number_instead_of_a_wrong_one():
    reply = Reply("ok", tokens=Usage(input=1000), model="gpt-6-unreleased")
    assert CodexAdapter(executable=cli_stub()).price(reply) is None
