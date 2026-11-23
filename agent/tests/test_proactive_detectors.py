import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from parachute_client import NoteIndex
from proactive_intelligence.detectors import select,stamp,DAY

NOW=stamp('2026-10-11T18:00:00Z')
def note(id,kind,**m):
 return NoteIndex(id=id,updated_at='2026-10-11T17:00:00Z',metadata={'type':kind,**m})
class Detectors(unittest.TestCase):
 def test_important_not_raw_tag_relay(self):
  rows=[note('a','email',subject='Decision',sender='a@example.com'),note('b','email',sender='owner@example.com')]
  out=select(rows,NOW,score=lambda n:.8)
  self.assertTrue(out[0]['active']);self.assertEqual(out[0]['kind'],'unprocessed');self.assertFalse(out[1]['active'])
 def test_chat_meeting_proposals_are_reviewed_even_without_importance_score(self):
  rows=[
   note('mention','message-thread',lastMessageAt='2026-10-11T17:00:00Z',lastMessageText='Can you find us a time to chat next week? Alex can join as optional.'),
   note('options','message-thread',lastMessageAt='2026-10-11T17:00:00Z',lastMessageText='Would you share which options work? Wed Nov 11 at 11am MT or Thu Nov 19 at 10am MT.'),
   note('noise','message-thread',lastMessageAt='2026-10-11T17:00:00Z',lastMessageText='Thanks, I will read this tomorrow.'),
  ]
  out=select(rows,NOW,score=lambda n:0)
  self.assertEqual([x['active'] for x in out],[True,True,False])
  self.assertTrue(all(x['kind']=='unprocessed' for x in out[:2]))
  self.assertIn('scheduling',out[0]['content'].lower())
 def test_done_and_guessed_deadline_inactive(self):
  rows=[note('a','task',status='done',due='2026-10-11',deadline_source='stated'),note('b','task',due='2026-10-11',deadline_source='guessed'),note('c','task',due='2026-10-11',deadline_source='owner')]
  self.assertEqual([x['active'] for x in select(rows,NOW)],[False,False,True])
 def test_date_timezone_and_unknown_time(self):
  self.assertIsNone(stamp('2026-10-11T12:00:00'))
  self.assertEqual(stamp('2026-10-11',True),stamp('2026-10-11T23:59:00-06:00'))
 def test_cancelled_and_overlapping_events(self):
  rows=[note('a','meeting',calendarEventId='a',start='2026-10-13T12:00:00Z',end='2026-10-13T13:00:00Z'),note('b','meeting',calendarEventId='b',start='2026-10-13T12:30:00Z',end='2026-10-13T13:30:00Z'),note('c','meeting',calendarEventId='c',start='2026-10-11T19:00:00Z',end='2026-10-11T20:00:00Z',event_status='cancelled')]
  out=select(rows,NOW)
  self.assertEqual([x['active'] for x in out],[True,True,False]);self.assertIn('overlap',out[0]['content'])
 def test_calendar_travel_offer_not_booking(self):
  n=note('trip-event','meeting',calendarEventId='event1',title='Conference trip',start='2026-11-01T12:00:00Z',end='2026-11-03T12:00:00Z',location='A city')
  e=select([n],NOW)[0]
  self.assertTrue(e['active']);self.assertEqual(e['kind'],'travel');self.assertIn('existing bookings',e['content'])
 def test_watch_deadline_does_not_fire_from_body_change(self):
  n=note('a','document',due='2026-10-15')
  w={'source_id':'a','revision':'old','condition':'deadline_changed'}
  import json
  out=select([n],NOW,watchers=[w],deadline_baselines={'a':json.dumps([None,'2026-10-15',None])})
  self.assertEqual(out,[])
  out=select([n],NOW,watchers=[w],deadline_baselines={'a':'old'})
  self.assertTrue(out[0]['active'])
 def test_cached_body_unchanged_and_bounded_reads(self):
  from unittest.mock import patch
  from tempfile import TemporaryDirectory
  from types import SimpleNamespace
  from proactive_management import Management
  from proactive_intelligence.detectors import collect
  from parachute_client import Note
  class Client:
   def __init__(self):self.reads=0
   def query_notes(self,**kw):return [note('email-'+str(i),'email',subject='Important',sender='person@example.com') for i in range(42)]
   def get_note(self,sid):
    self.reads+=1
    return Note(id=sid,updated_at='2026-10-11T17:00:00Z',content='Human readable message')
  with TemporaryDirectory() as d, patch('standing_agent.relevance.build_context',return_value=None), patch('standing_agent.relevance.score_inbound',return_value=SimpleNamespace(score=.9)):
   m=Management(str(Path(d)/'state.sqlite3'));c=Client()
   first=collect(c,NOW,m)[0];self.assertEqual(c.reads,40);self.assertEqual(first['health'],'healthy');self.assertEqual(len(first['events']),40)
   second=collect(c,NOW,m)[0];self.assertEqual(c.reads,42);self.assertEqual(len(second['events']),42)
   collect(c,NOW,m);self.assertEqual(c.reads,42)
   m.db.close()
if __name__=='__main__':unittest.main()
