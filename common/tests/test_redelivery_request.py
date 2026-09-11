"""验证补发货调用参数与内部发货接口的实际模型兼容。"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parents[2]


class RedeliveryRequestTest(unittest.TestCase):
    def test_scheduler_payload_passes_receiver_validation_for_each_account(self):
        scheduler = ast.parse((ROOT / 'scheduler/app/services/scheduler/redelivery_task.py').read_text(encoding='utf-8-sig'))
        payload_node = next(
            node.value for node in ast.walk(scheduler)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'deliver_data' for target in node.targets)
        )
        receiver = ast.parse((ROOT / 'websocket/app/api/routes/internal.py').read_text(encoding='utf-8-sig'))
        model_node = next(node for node in receiver.body if isinstance(node, ast.ClassDef) and node.name == 'DeliverOrderRequest')
        namespace = {'BaseModel': BaseModel}
        exec(compile(ast.Module(body=[model_node], type_ignores=[]), '<receiver>', 'exec'), namespace)
        model = namespace['DeliverOrderRequest']
        for account_id in ('seller-a', 'seller-b'):
            with self.subTest(account_id=account_id):
                payload = eval(compile(ast.Expression(payload_node), '<scheduler-payload>', 'eval'), {
                    'account_id': account_id,
                    'order_no': 'same-order',
                    'order': SimpleNamespace(item_id='item-1', buyer_id='buyer-1', chat_id='chat-1', is_bargain=False),
                    'card': SimpleNamespace(id=3),
                    'order_quantity': 2,
                })
                request = model.model_validate(payload)
                self.assertEqual(request.account_id, account_id)
                self.assertEqual(request.item_id, 'item-1')
                self.assertEqual(request.quantity, 2)
                self.assertEqual(request.delivery_method, 'scheduled')
                del payload['account_id']
                with self.assertRaises(ValidationError):
                    model.model_validate(payload)


if __name__ == '__main__':
    unittest.main()
