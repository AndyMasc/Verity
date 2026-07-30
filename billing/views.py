from django.shortcuts import render
from djstripe.models import Product


def pricing_page(request):
    return render(request, 'billing/pricing_page.html', {
        'products': Product.objects.all()
    })