=============
Drilldown API
=============

Extends Django REST Framework to create instant full-featured GET APIs with fields, filters, offset,
limit, etc., including the ability to access chained objects (foreignKey, manyToMany, oneToOne fields).

You create just one simple view per API; you do not need to create separate serializers for the
chained objects.


Quickstart
----------
Example adapted from code in test.py.

1. Create a view that's a subclass of DrillDownAPIView (use your own models; "Invoice" is just an example)::

    from rest_framework_drilldown import DrillDownAPIView

    class InvoiceList(DrillDownAPIView):
        """A GET API for Invoice objects"""
        # Primary model for the API (required)
        model = Invoice

        # Optional list of chained foreignKey, manyToMany, and oneToOne objects
        # your users can drill down into -- note that you do not need to build
        # separate serializers for these; DrilldownAPI builds them dynamically.
        drilldowns = ['client__profile', 'salesperson__profile', 'items']

        # Optional list of fields to ignore if they appear in the request
        ignore = ['fakefield']

        # Optional list of fields that your users are not allowed to
        # see or query
        hide = ['salesperson__commission_pct']

        def get_base_query(self):
            # Base query for your class, typically just '.objects.all()'
            return Invoice.objects.all()


2. In urls.py, create a URL for the view::
    url(r'^invoices/$', InvoiceList.as_view(), name='invoices'),

3. Start running queries! Some of the things you can do:

* Limit and offset:
    ``/invoices/?limit=10&offset=60``

    Does just what you'd expect. The total number of results is returned in a custom header code: ``X-Total-Count: 2034``

* Specify fields to include, including "drilldown" fields:
    ``/invoices/?fields=id,client.profile.first_name,client.profile.last_name``

    (invoices showing just the invoice ID and the client's first and last name)

* Filter on fields:
    ``/invoices/?total__gte=100&salesperson.last_name__iexact=smith``

    (invoices where total >= $100 and salesperson is "Smith")

* Use the 'ALL' keyword to return all fields in an object:
    ``/invoices/?fields=salesperson.ALL``

    (list the salesperson for each invoice; will display all salesperson fields
    EXCEPT commission_pct which is in the "hide" list in the API above)

* Use order_by, including - sign for reverse:
    ``/invoices/?order_by=client.profile.last_name,-amount``

    (invoices ordered by associated client's last name, from highest to lowest amount)

Total number of results for each query (before applying limit and offset) are returned in a custom header code:
    ``X-Total-Count: 2034``


Errors are also returned in a custom header code, usually with status 400:
    ``X-Query_Error: error text``

Also supports format parameter, e.g. ?format=json

Solutions for Common Problems
-----------------------------
* Access Control:
    Override the get() method in the API view and add your access control to it::

        @method_decorator(accounting_permission_required)
        def get(self, request):
            return super(InvoiceList, self).get(request)


* Custom Queries:
    Assume that invoices > $1000 require prior authorization, and you'd like to support that as a simple query:

    ``invoices?requires_authorization=True``

    1. Add 'requires_authorization' to the ignore list in the API view:
        ``ignore = ['fakefield', 'requires_authorizaton']``

    2. Add the logic to ``get_base_query()`` in the API view::

        def get_base_query(self):
            qs = Invoice.objects.all()
            if self.request.GET.get('requires_authorizaton'):
                requires_authorization = self.request.GET['requires_authorization']
                if requires_authorization == 'True':
                    qs = qs.filter(total__gt=1000)
                elif requires_authorization == 'False':
                    qs = qs.exclude(total__gt=1000)
            return qs

    Now you can query for ``requires_authorization=True`` or ``requires_authorization=False``.