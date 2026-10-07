"""Static batch checks before any proposal is written.

Runtime values, authority and currentness are still checked by each invoke.
"""
import inspect
from .common import Fault, need, obj, text


def preflight(control, actor, actions, allowed, forbidden='workflow_boundary'):
    labels = set()

    def references(value, depth=0):
        need(depth < 30, 'invalid_reference', 'Nested reference too deep')
        if isinstance(value, dict) and set(value) == {'$ref'}:
            text(value['$ref'], 'result reference', 1000)
            parts = value['$ref'].split('.')
            need(all(parts) and parts[0] in labels, 'missing_reference',
                 'Reference must name an earlier action label')
        elif isinstance(value, dict):
            for child in value.values(): references(child, depth+1)
        elif isinstance(value, list):
            for child in value: references(child, depth+1)

    for index, action in enumerate(actions):
        try:
            obj(action, required=('method', 'params'), optional=('as',))
            text(action['method'], 'method', 100)
            need(action['method'] in allowed, forbidden, 'Operation is outside the proposal boundary')
            params = action['params']
            need(isinstance(params, dict), 'invalid_params', 'Expected a JSON object')
            need(not {'actor','requester','security','store'} & params.keys(),
                 'forbidden', 'Transport identity cannot be supplied')
            need(action['method'] in control.routes, 'unknown_method', 'Method is not exported')
            # A whole params object may come from an earlier result. Its shape
            # is unknown until resolve/invoke; statically check the reference only.
            if set(params) != {'$ref'}:
                try: inspect.signature(control.routes[action['method']]).bind(actor, **params)
                except TypeError as exc:
                    raise Fault('invalid_params', 'Arguments do not match the method contract', str(exc)) from exc
            references(params)
            if 'as' in action:
                text(action['as'], 'action label', 200)
                need('.' not in action['as'], 'invalid_reference', 'Labels cannot contain dots')
                need(action['as'] not in labels, 'duplicate_label', 'Action labels cannot be overwritten')
                labels.add(action['as'])
        except Fault as exc:
            return {'method': action.get('method') if isinstance(action, dict) else None,
                    'index': index, 'error': exc.as_dict(), 'preflight': True}
    return None
