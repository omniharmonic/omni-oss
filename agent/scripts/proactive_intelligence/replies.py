"""Fresh read-only Sent corroboration. Missing/incomplete evidence is unknown."""
import json
import multiprocessing
import os
import sys


def _sent_worker(connection, accounts):
    try:
        sys.stdout=open(os.devnull,'w'); sys.stderr=open(os.devnull,'w')
        from omni_nudges import sent_evidence
        diagnostics={}
        result=sent_evidence(accounts,diagnostics)
        connection.send(result)
    except BaseException:
        connection.send({account:None for account in accounts})
    finally: connection.close()


class ReplyGuard:
    def __init__(self, fetch=None, timeout=90, max_accounts=2):
        self.fetch=fetch or self._fetch
        self.timeout=min(timeout,90); self.max_accounts=min(max_accounts,2)

    def _fetch(self, accounts):
        context=multiprocessing.get_context('spawn')
        parent,child=context.Pipe(False)
        process=context.Process(target=_sent_worker,args=(child,accounts))
        process.start(); child.close()
        try:
            if not parent.poll(self.timeout): return {account:None for account in accounts}
            return parent.recv()
        except (EOFError,OSError): return {account:None for account in accounts}
        finally:
            if process.is_alive(): process.terminate()
            process.join(timeout=5)
            if process.is_alive(): process.kill(); process.join()
            parent.close()

    def check(self, events):
        from omni_nudges import replied_since
        mails=[e for e in events if e.get('channel')=='email']
        accounts=sorted({e.get('mail_account','') for e in mails if e.get('mail_account')})[:self.max_accounts]
        sent=self.fetch(accounts) if accounts else {}
        eligible,replied,unknown=[],[],[]
        for e in events:
            if e.get('channel')!='email': eligible.append(e); continue
            account=e.get('mail_account'); thread=e.get('mail_thread_id'); when=e.get('inbound_at')
            evidence=sent.get(account)
            if not account or not thread or not isinstance(when,(int,float)) or evidence is None or not isinstance(evidence,dict):
                unknown.append(e); continue
            if replied_since(evidence,thread,when):
                # The sent time (the thread's latest Sent message) rides along: the scheduling latch starts there.
                at=evidence.get(thread)
                replied.append(dict(e,replied_at=at) if isinstance(at,int) and not isinstance(at,bool) else e)
            else: eligible.append(e)
        return {'eligible':eligible,'replied':replied,'unknown':unknown}
