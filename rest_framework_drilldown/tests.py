from decimal import Decimal
from django.db import models
from django.conf import settings
from django.test import TestCase
from django.test.client import RequestFactory

from .views import DrillDownAPIView


# Create some database models
class test_Profile(models.Model):
    first_name = models.CharField(max_length=20)
    last_name = models.CharField(max_length=20)
    spy_name = models.CharField(max_length=10)


class test_Client(models.Model):
    wholesale = models.BooleanField(default=False)
    profile = models.ForeignKey(test_Profile)


class test_Salesperson(models.Model):
    commission_pct = models.IntegerField(default=10)
    profile = models.ForeignKey(test_Profile)


class test_Item(models.Model):
    description = models.TextField(max_length=100)
    price = models.DecimalField(decimal_places=2, max_digits=8, null=True)


class test_Invoice(models.Model):
    client = models.ForeignKey(test_Client)
    salesperson = models.ForeignKey(test_Salesperson, null=True)
    items = models.ManyToManyField(test_Item,  related_name='invoice')
    total = models.DecimalField(decimal_places=2, max_digits=8, default=Decimal('0'))

    def save(self, *args, **kwargs):
        if self.id:
            self.total = sum([i.price for i in self.items.all()])
        super(test_Invoice, self).save(*args, **kwargs)


# Build an API
class DrilldownTestAPI(DrillDownAPIView):
    """A GET API for test_Invoice objects"""
    authentication_classes = ()  # turn off authentication for the test
    permission_classes = ()

    model = test_Invoice
    drilldowns = ['client__profile', 'salesperson__profile', 'items']  # objects you are allowed to drill down in
    ignore = ['fakefield']  # fields to ignore in the request
    hide = ['salesperson__commission_pct']  # fields that you cannot access or use in filters

    def get_base_query(self):
        return test_Invoice.objects.all()


class DrilldownAPITest(TestCase):
    def setUp(self):
        mary_smith = test_Profile(last_name='Smith', first_name='Mary', spy_name='Mango')
        joe_dokes = test_Profile(last_name='Dokes', first_name='Joe', spy_name='Bravo')
        bob_dobbs = test_Profile(last_name='Dobbs', first_name='Bob', spy_name='Catgut')
        ann_ames = test_Profile(last_name='Ames', first_name='Ann', spy_name='Pegleg')
        for profile in [mary_smith, joe_dokes, bob_dobbs, ann_ames]:
            profile.save()

        client_mary = test_Client(wholesale=True, profile=mary_smith)
        client_joe = test_Client(wholesale=False, profile=joe_dokes)
        salesperson_bob = test_Salesperson(commission_pct=8, profile=ann_ames)
        salesperson_ann = test_Salesperson(profile=bob_dobbs)
        client_mary.save()
        client_joe.save()
        salesperson_bob.save()
        salesperson_ann.save()

        items = [('Tape', '2.20'), ('Dog bowl', '4.00'), ('Hat', '10.00'), ('Tire', '30.00'), ('Mouse', '20'),
                 ('Sandwich', '5.50'), ('Audi', '10000'), ('Coffee', '1.50'), ('Chair', '75'), ('Eggs', '3.50'),
                 ('Pencils', '0.05'), ('Spoon', '2.00'), ]
        for item in items:
            i = test_Item(description=item[0], price=Decimal(item[1]))
            i.save()

        def create_invoice(client, salesperson, items):
            i = test_Invoice(client=client, salesperson=salesperson)
            i.save()
            i.items.add(*items)
            i.save()

        items = test_Item.objects.all()
        create_invoice(client_mary, salesperson_bob, items[0:3])
        create_invoice(client_mary, salesperson_bob, items[3:4])
        create_invoice(client_joe, salesperson_ann, items[4:6])
        create_invoice(client_joe, None, items[6:9])
        create_invoice(client_joe, salesperson_ann, items[9:12])

        self.factory = RequestFactory()



    def test_the_api(self):
        my_view = DrilldownTestAPI.as_view()

        # set debug true so that API will return X-Query-Count (number of queries run)
        saved_debug = settings.DEBUG
        settings.DEBUG = True

        def get_response(data):
            return my_view(self.factory.get('/url/', data, content_type='application/json'))

        # return all results
        response = get_response({})
        self.assertEqual(len(response.data), 5)

        # a filter
        response = get_response({'salesperson.profile.first_name': 'Ann'})
        self.assertEqual(len(response.data), 2)

        # isnull
        response = get_response({'salesperson__isnull': 'true'})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(int(response.get('X-Query-Count', 0)), 1)  # should only need one query

        # fields
        response = get_response({
            'salesperson__isnull': 'false',
            'fields': 'salesperson.profile.first_name,salesperson.profile.last_name'
        })
        self.assertEqual(len(response.data[0]['salesperson']['profile']), 2)  # each record contains only the two fields
        self.assertEqual(int(response.get('X-Query-Count', 0)), 1)  # should only need one query

        # complicated
        response = get_response({
            'salesperson__isnull': 'false',
            'fields': 'salesperson.profile.first_name,items.price,client.profile.first_name'
        })
        self.assertIsInstance(response.data[0]['items'][0]['price'], Decimal)
        self.assertTrue(int(response.get('X-Query-Count', 0)) <= 2)  # should only need 1-2 queries

        # on a manytomany field  if you don't specify subfields, returns a flat list of ids
        response = get_response({'fields': 'items'})
        # data should look something like this: [{'items': [1, 2]}], NOT [{'items': [{'id': 1}, {'id': 2}]}]
        self.assertTrue(type(response.data[0]['items'][0]) is int)

        # try with limit
        response = get_response({'limit': 1})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(int(response.get('X-Total-Count', 0)), 5)

        # try with offset
        response = get_response({'offset': 3})
        self.assertEqual(len(response.data), 2)
        self.assertEqual(int(response.get('X-Total-Count', 0)), 5)

        # both, with arbitrary high limit
        response = get_response({'offset': 2, 'limit': 100})
        self.assertEqual(len(response.data), 3)
        self.assertEqual(int(response.get('X-Total-Count', 0)), 5)

        # zero results
        response = get_response({'salesperson.profile.first_name': 'Fred'})
        self.assertEqual(response.status_code, 200) # not an error
        self.assertEqual(len(response.data), 0)

        # a bad filter
        response = get_response({'salesperson.profile.dog_name': 'Freddyboy'})
        self.assertEqual(response.status_code, 400) # error
        self.assertTrue('dog_name' in response.get('X-Query_Error'))

        # an ignore field
        response = get_response({'fakefield__lt': '3000', 'limit': 3})
        self.assertEqual(response.status_code, 200) # no error, as 'fakefield' is in the ignore list
        self.assertEqual(len(response.data), 3)

        # ALL selector
        response = get_response({'fields': 'client.profile.ALL'})
        self.assertEqual(len(response.data[0]['client']['profile']), 4) # 4 fields including id

        # a hide field -- should not show up in results
        response = get_response({'salesperson__isnull': 'false', 'fields': 'salesperson.ALL'})
        self.assertIsNone(response.data[0]['salesperson'].get('commission_pct'))  # commission_pct is a hide field
        self.assertIsNotNone(response.data[0]['salesperson'].get('profile'))

        settings.DEBUG = saved_debug  # revert settings
