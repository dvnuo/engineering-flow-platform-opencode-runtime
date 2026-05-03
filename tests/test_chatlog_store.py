from efp_opencode_adapter.chatlog_store import ChatLogStore

def test_chatlog_store(tmp_path):
    s = ChatLogStore(tmp_path)
    s.start_entry('../bad', request_id='r1', message='hello')
    assert (tmp_path / 'bad.json').exists()
    s.finish_entry('../bad', request_id='r1', status='success', response='ok')
    d = s.get('../bad')
    assert d['entries']
    s2 = ChatLogStore(tmp_path)
    assert s2.get('../bad')['entries']
    s.start_entry('x', request_id='r2', message='m')
    assert s.latest_entry('x')['request_id'] == 'r2'
