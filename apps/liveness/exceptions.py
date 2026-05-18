import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger('apps.liveness')

def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is None:
        logger.exception('Unhandled exception in %s', context.get('view'))
        response = Response(
            {
                'success': False,
                'error': 'Internal server error.',
                'detail': str(exc),
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return response