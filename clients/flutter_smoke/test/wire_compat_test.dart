// Proves the Python server speaks the protocol the Dart package parses.
//
// The fixture is NOT hand-written — `scripts/capture_events.py` runs the real
// LangGraph agent and dumps every frame it emitted. So this test fails if the
// server's output drifts from `AgentEvent.fromJson`, which is the only failure
// mode worth a test here. A hand-written fixture would only prove the fixture
// matches the client.
//
// Run:  cd clients/flutter_smoke && flutter test

import 'dart:convert';
import 'dart:io';

import 'package:agent_kit/agent_kit.dart';
import 'package:flutter_test/flutter_test.dart';

List<Map<String, Object?>> loadFixture() {
  final File file = File('test/fixtures/events.jsonl');
  if (!file.existsSync()) {
    throw StateError(
      'test/fixtures/events.jsonl is missing. '
      'Regenerate it with: python scripts/capture_events.py',
    );
  }
  return file
      .readAsLinesSync()
      .where((final String line) => line.trim().isNotEmpty)
      .map((final String line) => jsonDecode(line) as Map<String, Object?>)
      .toList(growable: false);
}

void main() {
  final List<Map<String, Object?>> frames = loadFixture();

  test('fixture is non-trivial and covers every event type', () {
    expect(frames.length, greaterThan(20));
    final Set<Object?> types =
        frames.map((final Map<String, Object?> f) => f['type']).toSet();
    expect(types, containsAll(<String>['plan', 'thinking', 'tool_call', 'citations']));
  });

  test('every server frame decodes into an AgentEvent', () {
    for (final (int i, Map<String, Object?> frame) in frames.indexed) {
      expect(
        () => AgentEvent.fromJson(frame),
        returnsNormally,
        reason: 'frame $i did not decode: ${jsonEncode(frame)}',
      );
    }
  });

  test('each frame decodes to the subtype its type claims', () {
    for (final Map<String, Object?> frame in frames) {
      final AgentEvent event = AgentEvent.fromJson(frame);
      switch (frame['type']) {
        case 'tool_call':
          expect(event, isA<ToolCallEvent>());
        case 'thinking':
          expect(event, isA<ThinkingEvent>());
        case 'citations':
          expect(event, isA<CitationEvent>());
        case 'plan':
          expect(event, isA<PlanEvent>());
        default:
          fail('unexpected type ${frame['type']}');
      }
    }
  });

  test('round-trip is lossless: decode -> toJson equals the server frame', () {
    // Catches the subtle break: a field the server sends that the Dart model
    // silently drops would pass `fromJson` and fail here.
    for (final Map<String, Object?> frame in frames) {
      expect(AgentEvent.fromJson(frame).toJson(), equals(frame));
    }
  });

  test('tool calls advance one card: stable ids, terminal states reached', () {
    final Map<String, List<ToolCallState>> byId = <String, List<ToolCallState>>{};
    for (final Map<String, Object?> frame in frames) {
      if (frame['type'] != 'tool_call') continue;
      final ToolCall call = (AgentEvent.fromJson(frame) as ToolCallEvent).call;
      byId.putIfAbsent(call.id, () => <ToolCallState>[]).add(call.state);
    }

    expect(byId, isNotEmpty);
    for (final MapEntry<String, List<ToolCallState>> entry in byId.entries) {
      // A stable id is what lets AgentView update one card instead of
      // appending three. Each id must end in a terminal state.
      expect(
        entry.value.last.isTerminal,
        isTrue,
        reason: '${entry.key} never reached a terminal state: ${entry.value}',
      );
      // pending/running must never follow a terminal state for the same id.
      final int firstTerminal =
          entry.value.indexWhere((final ToolCallState s) => s.isTerminal);
      expect(
        entry.value.skip(firstTerminal + 1).any((final ToolCallState s) => !s.isTerminal),
        isFalse,
        reason: '${entry.key} went backwards out of a terminal state: ${entry.value}',
      );
    }
  });

  test('the write tool was approved and the read tools succeeded', () {
    final Iterable<ToolCall> calls = frames
        .where((final Map<String, Object?> f) => f['type'] == 'tool_call')
        .map((final Map<String, Object?> f) =>
            (AgentEvent.fromJson(f) as ToolCallEvent).call);

    expect(calls.map((final ToolCall c) => c.name).toSet(),
        containsAll(<String>['web_search', 'query_tickets', 'save_report']));

    final ToolCall save = calls.lastWhere((final ToolCall c) => c.name == 'save_report');
    expect(save.state, ToolCallState.success);
    expect(save.durationMs, isNotNull);
  });

  test('plan steps only move forward and all terminate', () {
    final List<PlanEvent> planEvents = frames
        .where((final Map<String, Object?> f) => f['type'] == 'plan')
        .map((final Map<String, Object?> f) => AgentEvent.fromJson(f) as PlanEvent)
        .toList(growable: false);

    expect(planEvents, isNotEmpty);
    final PlanEvent last = planEvents.last;
    expect(last.steps, isNotEmpty);
    for (final PlanStep step in last.steps) {
      expect(step.state.isTerminal, isTrue,
          reason: '${step.id} ended as ${step.state}');
    }
  });

  test('citations carry a usable non-empty url', () {
    // Citation.url is non-nullable in Dart; an empty one renders a dead chip.
    final Iterable<Citation> citations = frames
        .where((final Map<String, Object?> f) => f['type'] == 'citations')
        .expand((final Map<String, Object?> f) =>
            (AgentEvent.fromJson(f) as CitationEvent).citations);

    expect(citations, isNotEmpty);
    for (final Citation c in citations) {
      expect(c.url, isNotEmpty);
      expect(Uri.tryParse(c.url), isNotNull);
      expect(c.title, isNotEmpty);
    }
  });
}
